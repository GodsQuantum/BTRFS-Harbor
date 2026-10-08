import { describe, expect, it } from 'vitest';
import {
	recoveryIntentNeedsNewHostname,
	recoveryIntentTitle,
	groupRecoveryActions,
	validateMigrationHostname,
	type MachineRecoveryPlan
} from './recovery';

describe('machine-aware recovery presentation', () => {
	it('requires a distinct hostname only for migration', () => {
		expect(recoveryIntentNeedsNewHostname('replace_machine')).toBe(false);
		expect(recoveryIntentNeedsNewHostname('migrate_machine')).toBe(true);
	});
	it('rejects a migration hostname identical to the source', () => {
		expect(validateMigrationHostname('Workstation-A', 'Workstation-A')).toMatch(/differ/i);
		expect(validateMigrationHostname('Workstation-A', 'Workstation-B')).toBe('');
	});
	it('uses user-facing recovery intent titles', () => {
		expect(recoveryIntentTitle('replace_machine')).toContain('Replace');
		expect(recoveryIntentTitle('migrate_machine')).toContain('another machine');
	});
	it('groups recovery actions by review section', () => {
		const plan: MachineRecoveryPlan = {
			intent: 'migrate_machine',
			hostname: 'ASUS-N55SF',
			compatibility: 'full_system',
			requires_rescue_environment: true,
			actions: [
				{ id: 'user_data', group: 'restored', description: 'Restore user data.' },
				{ id: 'hostname', group: 'adapted', description: 'Apply new hostname.' },
				{ id: 'machine_id', group: 'regenerated', description: 'Generate machine identity.' },
				{ id: 'network_profiles', group: 'attention', description: 'Review network profiles.' }
			]
		};
		const grouped = groupRecoveryActions(plan);
		expect(grouped.restored).toHaveLength(1);
		expect(grouped.adapted).toHaveLength(1);
		expect(grouped.regenerated).toHaveLength(1);
		expect(grouped.attention).toHaveLength(1);
	});
});
