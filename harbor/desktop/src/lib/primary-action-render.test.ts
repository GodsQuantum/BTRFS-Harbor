import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import Page from '../routes/+page.svelte';
import { demoStatus, type DashboardStatus } from './status';

describe('Btrfs Harbor primary backup action regression', () => {
	it.each(['setup', 'live', 'demo', 'offline'] as DashboardStatus['source'][])(
		'renders the backup button when dashboard is %s',
		(status) => {
			const previous = demoStatus.source;
			try {
				demoStatus.source = status;
				const { body } = render(Page);
				expect(body).toContain('data-testid="backup-now-primary"');
				expect(body).toContain('Back up now');
				expect(body).toContain('Destination');
				expect(body).toContain('Restore');
				if (status === 'setup') {
					expect(body).toContain('id="harbor-source-setup"');
				}
			} finally {
				demoStatus.source = previous;
			}
		}
	);
});
