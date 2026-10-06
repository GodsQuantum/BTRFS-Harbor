export type DestinationKind = 'local' | 'raw' | 'nfs' | 'smb' | 'ssh';

export type SourceHint = 'recommended' | 'usually_disposable' | 'optional';

export interface DiscoveredSource {
	mount_point: string;
	source: string;
	subvolume: string | null;
	snapper_config: string | null;
	snapshot_count?: number;
	sendable_snapshot_count?: number;
	latest_snapshot_number?: number | null;
	hint: SourceHint;
}

export interface DestinationSpec {
	id: string;
	name: string;
	kind: DestinationKind;
	path: string;
	mount_point: string | null;
	expected_mount_source: string | null;
	compression: string;
	optional: boolean;
}

export interface BackupSource {
	path: string;
	snapshot_prefix: string;
	snapper_config: string | null;
	target_subdir: string;
}

export interface RetentionPolicy {
	hourly: number;
	daily: number;
	weekly: number;
	monthly: number;
	yearly: number;
}

export interface BackupProfile {
	id: string;
	name: string;
	sources: BackupSource[];
	destination_ids: string[];
	on_calendar: string;
	retention: RetentionPolicy;
	verify_after_backup: boolean;
}

export interface HarborConfig {
	destinations: DestinationSpec[];
	profiles: BackupProfile[];
}

export type DraftIssue =
	| 'profile_name'
	| 'schedule'
	| 'sources'
	| 'destination'
	| 'destination_name'
	| 'destination_path'
	| 'mount_point'
	| 'expected_mount_source'
	| 'compression';

export type DraftSection = 'profile' | 'schedule' | 'sources' | 'destination';

export interface DraftIssueDescriptor {
	section: DraftSection;
	messageKey:
		| 'validationProfileRequired'
		| 'validationScheduleRequired'
		| 'validationSourcesRequired'
		| 'validationDestinationRequired'
		| 'validationDestinationUnavailable'
		| 'validationCompressionRequired';
}

export function describeDraftIssue(issue: DraftIssue): DraftIssueDescriptor {
	switch (issue) {
		case 'profile_name':
			return { section: 'profile', messageKey: 'validationProfileRequired' };
		case 'schedule':
			return { section: 'schedule', messageKey: 'validationScheduleRequired' };
		case 'sources':
			return { section: 'sources', messageKey: 'validationSourcesRequired' };
		case 'mount_point':
		case 'expected_mount_source':
			return { section: 'destination', messageKey: 'validationDestinationUnavailable' };
		case 'compression':
			return { section: 'destination', messageKey: 'validationCompressionRequired' };
		case 'destination':
		case 'destination_name':
		case 'destination_path':
			return { section: 'destination', messageKey: 'validationDestinationRequired' };
	}
}

const recommendedSources: Readonly<Record<string, BackupSource>> = {
	'/': {
		path: '/',
		snapshot_prefix: 'root-',
		snapper_config: 'root',
		target_subdir: 'rootfs'
	},
	'/home': {
		path: '/home',
		snapshot_prefix: 'home-',
		snapper_config: null,
		target_subdir: 'home'
	},
	'/root': {
		path: '/root',
		snapshot_prefix: 'root-home-',
		snapper_config: null,
		target_subdir: 'root-home'
	},
	'/srv': {
		path: '/srv',
		snapshot_prefix: 'srv-',
		snapper_config: null,
		target_subdir: 'srv'
	}
};

const recommendedOrder = Object.keys(recommendedSources);

function copySource(source: BackupSource): BackupSource {
	return { ...source };
}

function safeSourceStem(path: string): string {
	if (path === '/') return 'root';
	const parts = path.split('/').filter(Boolean);
	return parts.length > 0 ? parts.join('-') : 'source';
}

export function backupSourceFromDiscovery(source: DiscoveredSource): BackupSource {
	const stem = safeSourceStem(source.mount_point);
	return {
		path: source.mount_point,
		snapshot_prefix: stem + '-',
		snapper_config: source.snapper_config,
		target_subdir: source.mount_point === '/' ? 'rootfs' : stem
	};
}

export function createDefaultConfiguration(
	uuidFactory: () => string = () => crypto.randomUUID()
): HarborConfig {
	const profileId = uuidFactory();
	const destinationId = uuidFactory();

	return {
		destinations: [
			{
				id: destinationId,
				name: 'Backup destination',
				kind: 'raw',
				path: '',
				mount_point: null,
				expected_mount_source: null,
				compression: 'zstd',
				optional: false
			}
		],
		profiles: [
			{
				id: profileId,
				name: 'Recovery backup',
				sources: [],
				destination_ids: [destinationId],
				on_calendar: '*-*-* 02:00:00',
				retention: {
					hourly: 0,
					daily: 7,
					weekly: 4,
					monthly: 3,
					yearly: 0
				},
				verify_after_backup: true
			}
		]
	};
}

export function cloneConfiguration(config: HarborConfig): HarborConfig {
	return structuredClone(config);
}

export function resolveProfile(config: HarborConfig, profileId?: string): BackupProfile {
	const profile = profileId
		? config.profiles.find((candidate) => candidate.id === profileId)
		: config.profiles[0];
	if (!profile) throw new Error('No Harbor profile is configured.');
	return profile;
}

export function resolveDestination(config: HarborConfig, profile: BackupProfile): DestinationSpec {
	const destinationId = profile.destination_ids[0];
	const destination = config.destinations.find((candidate) => candidate.id === destinationId);
	if (!destination) throw new Error('The profile has no configured destination.');
	return destination;
}

export function draftIssues(config: HarborConfig, profile: BackupProfile): DraftIssue[] {
	const issues: DraftIssue[] = [];
	if (!profile.name.trim()) issues.push('profile_name');
	if (!profile.on_calendar.trim()) issues.push('schedule');
	if (profile.sources.length === 0) issues.push('sources');

	let destination: DestinationSpec | undefined;
	try {
		destination = resolveDestination(config, profile);
	} catch {
		issues.push('destination');
		return issues;
	}

	if (!destination.name.trim()) issues.push('destination_name');
	if (!destination.path.trim()) issues.push('destination_path');
	if (!destination.compression.trim()) issues.push('compression');

	if (destination.kind === 'nfs' || destination.kind === 'smb') {
		if (!destination.mount_point?.trim()) issues.push('mount_point');
		if (!destination.expected_mount_source?.trim()) issues.push('expected_mount_source');
	}

	return issues;
}

export function setSourceEnabled(
	profile: BackupProfile,
	path: string,
	enabled: boolean,
	template?: BackupSource
): BackupProfile {
	const existing = profile.sources.some((source) => source.path === path);
	if (enabled && existing) return profile;
	if (!enabled) {
		return {
			...profile,
			sources: profile.sources.filter((source) => source.path !== path)
		};
	}

	const sourceTemplate = template ?? recommendedSources[path];
	if (!sourceTemplate) throw new Error('No source template is available for: ' + path);

	const sources = [...profile.sources, copySource(sourceTemplate)].sort((a, b) => {
		const ai = recommendedOrder.indexOf(a.path);
		const bi = recommendedOrder.indexOf(b.path);
		if (ai === -1 && bi === -1) return a.path.localeCompare(b.path);
		if (ai === -1) return 1;
		if (bi === -1) return -1;
		return ai - bi;
	});

	return { ...profile, sources };
}

export function isSourceEnabled(profile: BackupProfile, path: string): boolean {
	return profile.sources.some((source) => source.path === path);
}

export const recommendedSourcePaths = [...recommendedOrder];
