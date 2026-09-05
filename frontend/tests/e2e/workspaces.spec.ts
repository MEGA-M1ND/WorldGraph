/**
 * End-to-end validation of the workspace switch, in a real browser.
 *
 * The claim under test is the one the Reality Pass exists to establish: WorldGraph can
 * open an estate that is not AtlasPay, run its ordinary workflows against it, and say
 * honestly what it does not know about it.
 *
 * Requires the backend to expose a second workspace — run it with
 * `WORLDGRAPH_AZURE_SNAPSHOT_PATH` pointing at `backend/tests/fixtures/azure_snapshot.json`.
 * Without one the switch cannot be exercised, and these tests skip rather than pass
 * vacuously: a green tick for a check that never ran is worse than a missing one.
 */

import { expect, test, type Page } from '@playwright/test';

const SHOTS = 'test-results/screenshots';

async function waitForBoot(page: Page): Promise<void> {
  await expect(page.locator('[data-bind="globe-loading"]')).toBeHidden({ timeout: 30_000 });
}

async function dismissFirstRun(page: Page): Promise<void> {
  const dialog = page.locator('[data-bind="first-run"]');
  if (await dialog.isVisible()) {
    await page.click('[data-action="dismiss-first-run"]');
    await expect(dialog).toBeHidden();
  }
}

/** Options in the workspace selector, excluding the demo. */
async function importableWorkspaces(page: Page): Promise<string[]> {
  return page.locator('[data-bind="workspace-select"] option').evaluateAll((options) =>
    options
      .map((option) => (option as HTMLOptionElement).value)
      .filter((value) => value !== 'atlaspay-demo'),
  );
}

async function switchTo(page: Page, workspaceId: string): Promise<void> {
  await page.selectOption('[data-bind="workspace-select"]', workspaceId);
  // The import reads the whole inventory before the graph appears.
  await expect(page.locator('[data-bind="coverage-panel"]')).toBeVisible({ timeout: 60_000 });
}

test.describe('workspace switching', () => {
  test('the demo estate is the default and declares itself synthetic', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    await expect(page.locator('[data-bind="org-name"]')).toHaveText('AtlasPay');
    await expect(page.locator('[data-bind="workspace-badge"]')).toHaveText('SYNTHETIC ESTATE');
    // A fixture that declares everything has no coverage story to tell.
    await expect(page.locator('[data-bind="coverage-panel"]')).toBeHidden();
  });

  test('switching to an imported estate replaces everything on screen', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    const others = await importableWorkspaces(page);
    test.skip(others.length === 0, 'no second workspace configured on this backend');

    // Select an entity first, so the test can prove the selection does not survive.
    const firstEntity = page.locator('[data-bind="event-list"] .row').first();
    if (await firstEntity.isVisible()) await firstEntity.click();

    await switchTo(page, others[0]!);

    // The organisation name comes from the workspace, not from a hardcoded string.
    await expect(page.locator('[data-bind="org-name"]')).not.toHaveText('AtlasPay');
    // Read-only, and labelled as such wherever the operator can see it.
    await expect(page.locator('[data-bind="workspace-badge"]')).toContainText('READ ONLY');

    // Nothing from the previous estate survived the switch.
    await expect(page.locator('[data-bind="detail-body"]')).not.toContainText('AtlasPay');
    await expect(page.locator('.sim-banner')).toBeHidden();

    await page.screenshot({ path: `${SHOTS}/workspace-imported.png`, fullPage: false });
  });

  test('the imported estate reports what WorldGraph does not know', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    const others = await importableWorkspaces(page);
    test.skip(others.length === 0, 'no second workspace configured on this backend');
    await switchTo(page, others[0]!);

    const coverage = page.locator('[data-bind="coverage-body"]');
    await expect(coverage).toBeVisible();
    // Coverage is reported dimension by dimension, never as one confidence score.
    await expect(coverage.locator('.coverage__row')).not.toHaveCount(0);
    const levels = await coverage
      .locator('.coverage__row')
      .evaluateAll((rows) => rows.map((row) => (row as HTMLElement).dataset['level']));
    expect(levels.every((level) => ['HIGH', 'PARTIAL', 'LOW', 'NONE'].includes(level ?? ''))).toBe(
      true,
    );
    // At least one dimension must be missing — the fixture declares almost no business
    // metadata, and a graph coverage report that claimed otherwise would be lying.
    expect(levels.some((level) => level === 'NONE' || level === 'LOW')).toBe(true);

    await page.screenshot({ path: `${SHOTS}/workspace-coverage.png`, fullPage: false });
  });

  test('an estate with no business metadata never invents a number', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    const others = await importableWorkspaces(page);
    test.skip(others.length === 0, 'no second workspace configured on this backend');
    await switchTo(page, others[0]!);

    const stats = page.locator('[data-bind="headline-stats"]');
    await expect(stats).toBeVisible();
    // innerText returns the CSS-uppercased label, so compare case-insensitively.
    const statText = (await stats.innerText()).toLowerCase();

    // The top bar shows infrastructure availability, labelled as such, rather than a
    // customer-experienced figure it has no basis for. (REALITY_PASS_AUDIT.md, C1.)
    expect(statText).toContain('infra availability');
    // And it is a real number, not a placeholder: the graph alone supports it.
    expect(statText).toMatch(/\d+\.\d\d%/);
  });

  test('analysing an imported entity stays inside that estate', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    const others = await importableWorkspaces(page);
    test.skip(others.length === 0, 'no second workspace configured on this backend');
    await switchTo(page, others[0]!);

    // Ask the analyst about the estate it is actually looking at.
    await page.fill('[data-bind="command-input"]', 'What can hurt us right now?');
    await page.press('[data-bind="command-input"]', 'Enter');
    await expect(page.locator('.message--analyst').last()).toBeVisible({ timeout: 45_000 });

    const reply = await page.locator('.message--analyst .message__text').last().innerText();
    // No leakage from the demo fixture, and no claim to have done anything: this is a
    // read-only view of somebody's real inventory.
    expect(reply).not.toContain('AtlasPay');
    expect(reply.toLowerCase()).not.toMatch(/i (restarted|failed over|scaled|deployed)/);

    await page.screenshot({ path: `${SHOTS}/workspace-analyst.png`, fullPage: false });
  });

  test('switching back restores the demo estate cleanly', async ({ page }) => {
    await page.goto('/');
    await waitForBoot(page);
    await dismissFirstRun(page);

    const others = await importableWorkspaces(page);
    test.skip(others.length === 0, 'no second workspace configured on this backend');

    await switchTo(page, others[0]!);
    await page.selectOption('[data-bind="workspace-select"]', 'atlaspay-demo');

    await expect(page.locator('[data-bind="org-name"]')).toHaveText('AtlasPay', {
      timeout: 30_000,
    });
    await expect(page.locator('[data-bind="workspace-badge"]')).toHaveText('SYNTHETIC ESTATE');
    await expect(page.locator('[data-bind="coverage-panel"]')).toBeHidden();
    // The demo's own entities are back, so the graph really was reloaded rather than
    // filtered.
    await expect(page.locator('[data-bind="event-list"] .row').first()).toBeVisible();
  });
});
