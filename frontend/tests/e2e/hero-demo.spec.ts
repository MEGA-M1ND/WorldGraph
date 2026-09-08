/**
 * End-to-end validation of the V1 hero demo through the real UI.
 *
 * This drives the actual application against the actual backend. It exists because the
 * product's acceptance standard is explicit: a UI feature is not finished on the strength
 * of code review. Each test also captures a screenshot, which is what the UI acceptance
 * pass inspects for alignment, clipping, contrast and legibility.
 */

import { expect, test, type Page } from '@playwright/test';

const SHOTS = 'test-results/screenshots';

/** Wait for the globe to stop reporting that it is loading. */
async function waitForBoot(page: Page): Promise<void> {
  await expect(page.locator('[data-bind="globe-loading"]')).toBeHidden({ timeout: 30_000 });
  await expect(page.locator('[data-bind="event-list"] .row').first()).toBeVisible();
}

/** Dismiss the first-run launcher, which opens on a fresh session. */
async function dismissFirstRun(page: Page): Promise<void> {
  const dialog = page.locator('[data-bind="first-run"]');
  if (await dialog.isVisible()) {
    await page.click('[data-action="dismiss-first-run"]');
    await expect(dialog).toBeHidden();
  }
}

/**
 * Ask the analyst and wait for its reply to land in the transcript.
 *
 * Two things beyond the obvious, both learned from a CI failure that reported only
 * "expected 3, received 2" after waiting 45 seconds:
 *
 * 1. The wait must outlast the *client's* own request timeout (60 s). Timing out first
 *    turns "the request was still in flight" into an indistinguishable failure.
 * 2. If the request errored, the app renders a notice rather than a message, so the
 *    transcript count never moves. Reading that notice back turns an opaque timeout into
 *    the actual reason.
 */
async function ask(page: Page, question: string): Promise<string> {
  const before = await page.locator('.message--analyst').count();
  await page.fill('[data-bind="command-input"]', question);
  await page.press('[data-bind="command-input"]', 'Enter');
  try {
    await expect(page.locator('.message--analyst')).toHaveCount(before + 1, {
      timeout: 65_000,
    });
  } catch (error) {
    const notices = (await page.locator('[data-bind="notices"]').innerText()).trim();
    throw new Error(
      `The analyst never answered ${JSON.stringify(question)}.` +
        (notices ? ` The app reported: ${notices}` : ' No notice was shown either.'),
      { cause: error },
    );
  }
  return (await page.locator('.message--analyst .message__text').last().innerText()).trim();
}

test.describe('WorldGraph hero demo', () => {
  test('first run offers the demo scenarios', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);

    const dialog = page.locator('[data-bind="first-run"]');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('Taiwan Earthquake Scenario');
    await expect(dialog).toContainText('Singapore Region Outage');
    await expect(dialog).toContainText('Critical CVE Scenario');
    // The synthetic-data disclaimer is on the very first screen, not buried.
    await expect(page.locator('[data-bind="first-run-disclaimer"]')).toContainText('synthetic');

    await page.screenshot({ path: `${SHOTS}/01-first-run.png`, fullPage: false });
  });

  test('the estate loads with headline stats and labelled provenance', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    await expect(page.locator('[data-bind="org-name"]')).toHaveText('AtlasPay');
    // The synthetic badge is in the top bar, permanently.
    // The badge's tone is derived from the loaded workspace, not a fixed class.
    await expect(page.locator('[data-bind="workspace-badge"][data-tone="synthetic"]')).toBeVisible();

    const stats = page.locator('[data-bind="headline-stats"] .stat');
    await expect(stats).toHaveCount(6);
    await expect(stats.filter({ hasText: 'Critical services' })).toBeVisible();
    await expect(stats.filter({ hasText: 'Material risks' })).toBeVisible();

    // Every event row carries its provenance mode. No unlabelled data anywhere.
    const modes = await page.locator('[data-bind="event-list"] .row .badge[data-state]').allInnerTexts();
    expect(modes.length).toBeGreaterThan(0);
    for (const mode of modes) {
      expect(['LIVE', 'REPLAY', 'SIMULATED', 'SYNTHETIC']).toContain(mode);
    }

    await expect(page.locator('[data-bind="risk-list"] .row').first()).toBeVisible();
    await expect(page.locator('[data-bind="feed-list"] .feed').first()).toBeVisible();

    await page.screenshot({ path: `${SHOTS}/02-estate.png`, fullPage: false });
  });

  test('full hero flow: event → analysis → simulation → response plan', async ({ page }) => {
    test.slow(); // the propagation animation and two camera flights are deliberate
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    // -- 1. Select the Taiwan earthquake ---------------------------------------------
    await page.locator('[data-bind="event-list"] .row', { hasText: 'Hsinchu' }).first().click();
    const detail = page.locator('[data-bind="detail-body"]');
    await expect(detail).toContainText('Hsinchu', { timeout: 15_000 });
    await expect(detail).toContainText('Enterprise proximity');
    // A replayed fixture must never present as live.
    await expect(detail.locator('.badge[data-state="REPLAY"]')).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/03-event-selected.png` });

    // -- 2. Analyze impact -------------------------------------------------------------
    await page.getByRole('button', { name: 'Analyze impact' }).click();
    await expect(detail).toContainText('material risk', { timeout: 30_000 });
    await expect(detail).toContainText('HIGH');

    // The risk score shows its derivation, not just a number.
    await expect(detail.locator('.derivation')).toBeVisible();
    const contributions = await detail.locator('.derivation__points').allInnerTexts();
    expect(contributions.length).toBeGreaterThanOrEqual(3);
    for (const value of contributions) expect(value).toMatch(/^[+-]\d+$/);

    // Directly and indirectly exposed, with the critical path to customers.
    await expect(detail).toContainText('Directly exposed');
    await expect(detail).toContainText('Indirectly exposed');
    await expect(detail).toContainText('Critical paths');
    await expect(detail.locator('.path__hop').first()).toBeVisible();

    // Confidence names its evidence and its uncertainties.
    await expect(detail).toContainText('Confidence');
    await expect(detail.locator('.evidence li[data-kind="uncertain"]').first()).toBeVisible();

    // Modelled figures are labelled as such.
    await expect(detail).toContainText('MODELLED ESTIMATE');

    // The timeline recorded the whole investigation.
    await expect(page.locator('.timeline__entry').first()).toBeVisible();
    // The stage labels are uppercased by CSS, so compare on the normalized value.
    const stages = (await page.locator('.timeline__stage').allInnerTexts()).map((s) =>
      s.trim().toLowerCase(),
    );
    expect(stages).toContain('correlate');
    expect(stages).toContain('analyze');
    expect(stages).toContain('risk');

    await page.waitForTimeout(2500); // let the propagation animation settle for the shot
    await page.screenshot({ path: `${SHOTS}/04-blast-radius.png` });

    // -- 3. Simulation: Taiwan supplier unavailable ------------------------------------
    await page.locator('.dock__tab', { hasText: 'Simulation' }).click();
    await page.selectOption('.failure-picker select >> nth=0', { label: 'Taiwan Hardware Supplier' });
    await page.selectOption('.failure-picker select >> nth=1', 'DOWN');
    await page.getByRole('button', { name: 'Add failure' }).click();

    // SIMULATION MODE must be unmissable.
    const banner = page.locator('[data-bind="simulation-banner"]');
    await expect(banner).toBeVisible({ timeout: 30_000 });
    await expect(banner).toContainText('SIMULATION MODE');

    const compare = page.locator('.compare');
    await expect(compare).toBeVisible();
    // Two availabilities, kept apart: what customers would experience, and the state of
    // the infrastructure itself. Collapsing them into one row is what let an estate with
    // no customer metadata report 100% (docs/REALITY_PASS_AUDIT.md, B1).
    await expect(compare).toContainText('Customer availability');
    await expect(compare).toContainText('Infrastructure availability');
    await expect(compare).toContainText('APAC capacity');
    await expect(compare).toContainText('Material risk');
    await page.screenshot({ path: `${SHOTS}/05-simulation-one-failure.png` });

    // -- 4. Add the second failure -----------------------------------------------------
    await page.selectOption('.failure-picker select >> nth=0', { label: 'payments-k8s-singapore' });
    await page.getByRole('button', { name: 'Add failure' }).click();
    await expect(page.locator('.override')).toHaveCount(2, { timeout: 30_000 });

    // The comparison escalates rather than merely changing.
    const riskRow = compare.locator('tr', { hasText: 'Material risk' });
    await expect(riskRow).toContainText('LOW');
    await expect(riskRow).toContainText('CRITICAL');
    await expect(riskRow).toHaveAttribute('data-direction', 'worse');
    await expect(page.locator('.compare__sim').first()).toBeVisible();

    await page.waitForTimeout(2500);
    await page.screenshot({ path: `${SHOTS}/06-simulation-cascade.png` });

    // -- 5. "What should we do?" -------------------------------------------------------
    const answer = await ask(page, 'What should we do?');
    expect(answer).toContain('RESPONSE PLAN');
    // Every action carries a rationale, and nothing was executed.
    expect(answer).toMatch(/Why:/);
    expect(answer.toLowerCase()).toContain('executes nothing');

    // The same plan also renders structurally, with per-action urgency and rationale.
    await expect(page.locator('[data-bind="detail-title"]')).toHaveText('Response plan', {
      timeout: 30_000,
    });
    const actions = page.locator('.plan__action');
    await expect(actions.first()).toBeVisible();
    expect(await actions.count()).toBeGreaterThanOrEqual(3);
    for (const rationale of await page.locator('.plan__rationale').allInnerTexts()) {
      expect(rationale.trim().length).toBeGreaterThan(0);
    }
    // Every row states that nothing ran.
    for (const note of await page.locator('.plan__footnote').allInnerTexts()) {
      expect(note).toContain('not executed');
    }

    await page.screenshot({ path: `${SHOTS}/07-response-plan.png` });
  });

  test('the analyst answers hero questions and labels simulations', async ({ page }) => {
    test.slow();
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    const risks = await ask(page, 'What can hurt us right now?');
    expect(risks).toMatch(/material risks/i);
    expect(risks).toMatch(/Taiwan|Singapore/);

    const simulation = await ask(page, 'What happens if Singapore goes offline?');
    // A hypothetical must never read as the current state.
    expect(simulation).toContain('SIMULATION');
    expect(simulation).toContain('Nothing real has changed');
    expect(simulation).toContain('CURRENT vs SIMULATION');
    await expect(page.locator('[data-bind="simulation-banner"]')).toBeVisible();

    const security = await ask(page, 'Which vulnerable systems can reach payments?');
    expect(security).toContain('admin-api');
    expect(security).toContain('internal-auth');
    expect(security).toContain('payments-api');
    expect(security.toLowerCase()).toContain('not proof of exploitability');

    await page.screenshot({ path: `${SHOTS}/08-analyst.png` });
  });

  test('the composer accepts the next question the moment an answer lands', async ({ page }) => {
    test.slow();
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    const input = page.locator('[data-bind="command-input"]');
    const send = page.locator('[data-bind="command-send"]');

    // Deliberately the expensive path: this answer creates a scenario, so it is followed
    // by a scenario fetch, a comparison refresh, camera flights and a timeline refresh.
    await input.fill('What happens if Singapore goes offline?');
    await input.press('Enter');
    await expect(page.locator('.message--analyst')).toHaveCount(1, { timeout: 65_000 });

    // The answer is on screen, so the composer must be usable. It used to stay disabled
    // for the ~8 s of presentation work that followed — and because a disabled submit
    // button also suppresses a form's implicit submission, Enter did nothing whatsoever:
    // no request, no notice, the typed question just sitting in the box.
    //
    // The timeouts here are deliberately far below that window. Playwright retries
    // assertions, so a generous timeout would have waited out the defect and passed.
    await expect(send).toBeEnabled({ timeout: 1_000 });

    await input.fill('What should we do?');
    await input.press('Enter');
    // The submit handler clears the field synchronously, so an empty box is proof the
    // submission was accepted rather than swallowed.
    await expect(input).toHaveValue('', { timeout: 3_000 });
    await expect(page.locator('.message--analyst')).toHaveCount(2, { timeout: 65_000 });

    // The presentation work still happens — it was moved off the critical path, not
    // dropped. The plan renders structurally, and every action still says it was not run.
    await expect(page.locator('[data-bind="detail-title"]')).toHaveText('Response plan', {
      timeout: 30_000,
    });
    expect(await page.locator('.plan__action').count()).toBeGreaterThanOrEqual(3);
    for (const note of await page.locator('.plan__footnote').allInnerTexts()) {
      expect(note).toContain('not executed');
    }
  });

  test('the analyst refuses to invent infrastructure', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    const answer = await ask(page, 'Tell me about our Reykjavik quantum datacenter.');
    // It must not describe a facility that does not exist. The honest outcome is a
    // refusal or the capability list — never a plausible-sounding description.
    expect(answer.toLowerCase()).not.toContain('reykjavik quantum datacenter is');
    expect(answer).toMatch(/did not recognise|No entity|does not exist|I can:/i);
  });

  test('selecting an entity opens its detail panel with actions', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    await ask(page, 'Tell me about payments-k8s-singapore');
    const detail = page.locator('[data-bind="detail-body"]');

    await page.locator('.dock__tab', { hasText: 'Dependencies' }).click();
    await expect(page.locator('[data-bind="dock-body"]')).toContainText('Select an entity');

    // Reach the entity through an impacted-entity row instead, which is the real path.
    await page.locator('[data-bind="event-list"] .row', { hasText: 'Hsinchu' }).first().click();
    await page.getByRole('button', { name: 'Analyze impact' }).click();
    await expect(detail).toContainText('Directly exposed', { timeout: 30_000 });
    await detail.locator('.row', { hasText: 'Taiwan Hardware Supplier' }).first().click();

    await expect(detail).toContainText('Taiwan Hardware Supplier', { timeout: 15_000 });
    await expect(detail).toContainText('Criticality');
    await expect(detail.getByRole('button', { name: 'Trace dependencies' })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Simulate failure' })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Ask AI' })).toBeVisible();

    await page.screenshot({ path: `${SHOTS}/09-entity-detail.png` });
  });

  test('share links round-trip and reject malformed input', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    await page.locator('[data-bind="event-list"] .row', { hasText: 'Hsinchu' }).first().click();
    await expect(page.locator('[data-bind="detail-body"]')).toContainText('Hsinchu');

    // The address bar carries the selection, and never a credential.
    await page.waitForFunction(() => window.location.search.includes('e='));
    const url = page.url();
    expect(url).toContain('e=replay%3Ataiwan-m68');
    expect(url).not.toMatch(/key|token|secret/i);

    // Restoring skips the first-run launcher — the author already chose the experience.
    await page.goto(url);
    await waitForBoot(page);
    await expect(page.locator('[data-bind="first-run"]')).toBeHidden();
    await expect(page.locator('[data-bind="detail-body"]')).toContainText('Hsinchu', {
      timeout: 20_000,
    });

    // A malformed link is rejected whole, with a notice — never half-applied.
    await page.goto('/?e=not a valid id!!&c=broken');
    await waitForBoot(page);
    await expect(page.locator('.notice')).toContainText('Share link not understood');
  });

  test('executive and engineer modes both render', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    await page.locator('.mode-switch__button', { hasText: 'Engineer' }).click();
    await expect(page.locator('#app')).toHaveAttribute('data-view-mode', 'engineer');
    await page.screenshot({ path: `${SHOTS}/10-engineer-mode.png` });

    await page.locator('.mode-switch__button', { hasText: 'Executive' }).click();
    await expect(page.locator('#app')).toHaveAttribute('data-view-mode', 'executive');
  });

  test('no console errors during the core flow', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(message.text());
    });
    page.on('pageerror', (error) => errors.push(error.message));

    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);
    await page.locator('[data-bind="event-list"] .row').first().click();
    await page.waitForTimeout(1500);

    // Cesium logs benign asset warnings on some GPUs; only real failures matter here.
    const real = errors.filter(
      (message) => !/DeveloperError|WebGL|Failed to load resource.*favicon/i.test(message),
    );
    expect(real, `Console errors: ${real.join(' | ')}`).toHaveLength(0);
  });
});
