export type RecoveryIntent = 'replace_machine' | 'migrate_machine';
export type RecoveryCompatibility = 'full_system' | 'data_migration_only' | 'blocked';
export type RecoveryActionGroup = 'restored' | 'adapted' | 'regenerated' | 'attention';

export interface RecoveryIdentityAction {
	id: string;
	group: RecoveryActionGroup;
	description: string;
}

export interface MachineRecoveryPlan {
	intent: RecoveryIntent;
	hostname: string;
	compatibility: RecoveryCompatibility;
	requires_rescue_environment: boolean;
	actions: RecoveryIdentityAction[];
}

export interface MachineRecoveryRequest {
	intent: RecoveryIntent;
	source_hostname: string;
	requested_hostname: string | null;
	source_os_release: string;
	target_os_release: string;
	includes_system: boolean;
}

export interface RecoveryActionGroups {
	restored: RecoveryIdentityAction[];
	adapted: RecoveryIdentityAction[];
	regenerated: RecoveryIdentityAction[];
	attention: RecoveryIdentityAction[];
}

export function recoveryIntentNeedsNewHostname(intent: RecoveryIntent): boolean {
	return intent === 'migrate_machine';
}

export function recoveryIntentTitle(intent: RecoveryIntent): string {
	return intent === 'replace_machine' ? 'Replace this machine' : 'Migrate to another machine';
}

export function validateMigrationHostname(source: string, requested: string): string {
	const hostname = requested.trim();
	if (!hostname) return 'A new hostname is required for migration.';
	if (hostname.toLocaleLowerCase() === source.trim().toLocaleLowerCase()) {
		return 'The migration hostname must differ from the source machine hostname.';
	}
	if (hostname.length > 63 || hostname.startsWith('-') || hostname.endsWith('-')) {
		return 'Hostname must be 1���63 characters and cannot start or end with a hyphen.';
	}
	if (!/^[A-Za-z0-9-]+$/.test(hostname)) {
		return 'Hostname may contain only letters, digits and hyphens.';
	}
	return '';
}

export function groupRecoveryActions(plan: MachineRecoveryPlan): RecoveryActionGroups {
	const groups: RecoveryActionGroups = {
		restored: [],
		adapted: [],
		regenerated: [],
		attention: []
	};
	for (const action of plan.actions) groups[action.group].push(action);
	return groups;
}
