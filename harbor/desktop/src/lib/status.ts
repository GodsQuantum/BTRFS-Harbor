export interface TargetStatus {
	path: string;
	status: string;
	backup_count: number;
	pending: number;
}

export interface VolumeStatus {
	path: string;
	source: {
		status: string;
		snapshot_count: number;
		latest_snapshot: string | null;
	};
	targets: TargetStatus[];
}

export interface EngineStatus {
	schema_version: number;
	config?: string | null;
	healthy: boolean;
	volumes: VolumeStatus[];
	error?: {
		code: string;
		message: string;
	} | null;
}

export interface ProfileRuntime {
	profile_id: string;
	timer_unit: string;
	service_unit: string;
	timer_installed: boolean;
	timer_enabled: boolean;
	timer_active: boolean;
	timer_sub_state: string | null;
	next_elapse_realtime: string | null;
	last_trigger: string | null;
	service_active: boolean;
	service_sub_state: string | null;
	service_result: string | null;
	service_exit_status: number | null;
}

export interface BackupProgressEvent {
	event: 'phase' | 'output' | 'finished' | 'failed';
	phase: string;
	message: string;
	stream?: 'stdout' | 'stderr';
}

export function backupProgressPercent(event: BackupProgressEvent | null | undefined): number {
	if (!event) return 0;
	if (event.event === 'finished') return 100;
	switch (event.phase) {
		case 'start':
			return 2;
		case 'prepare':
			return 8;
		case 'engine':
			return 12;
		case 'mount_guard':
			return 20;
		case 'backup':
			return 65;
		case 'verify':
			return 86;
		case 'recovery_kit':
			return 96;
		case 'complete':
			return 100;
		default:
			return 5;
	}
}

export function runtimeRepresentsScheduledJob(runtime: ProfileRuntime | null | undefined): boolean {
	return Boolean(runtime?.timer_installed);
}

export type DataSource = 'live' | 'demo' | 'setup' | 'offline';

export interface DashboardStatus {
	source: DataSource;
	profileId?: string;
	engine: EngineStatus;
	runtime?: ProfileRuntime | null;
	destinationName: string;
	destinationOnline: boolean;
	lastBackup: string;
	lastVerify: string;
	nextRun: string;
	transferred: string;
	duration: string;
	chainHealthy: boolean;
	restoreTested: boolean;
	warning?: string;
}

export const demoStatus: DashboardStatus = {
	source: 'demo',
	profileId: '11111111-1111-4111-8111-111111111111',
	engine: {
		schema_version: 1,
		config: '/etc/btrfs-harbor/profiles.d/workstation-recovery.toml',
		healthy: true,
		volumes: [
			{
				path: '/',
				source: { status: 'ok', snapshot_count: 10, latest_snapshot: 'root-20261005T021000' },
				targets: [
					{
						path: 'raw:///mnt/backup-nas/harbor/workstation/rootfs',
						status: 'ok',
						backup_count: 10,
						pending: 0
					}
				]
			},
			{
				path: '/home',
				source: { status: 'ok', snapshot_count: 7, latest_snapshot: 'home-20261005T021100' },
				targets: [
					{
						path: 'raw:///mnt/backup-nas/harbor/workstation/home',
						status: 'ok',
						backup_count: 7,
						pending: 0
					}
				]
			},
			{
				path: '/root',
				source: {
					status: 'ok',
					snapshot_count: 7,
					latest_snapshot: 'root-home-20261005T021200'
				},
				targets: [
					{
						path: 'raw:///mnt/backup-nas/harbor/workstation/root-home',
						status: 'ok',
						backup_count: 7,
						pending: 0
					}
				]
			},
			{
				path: '/srv',
				source: { status: 'ok', snapshot_count: 7, latest_snapshot: 'srv-20261005T021300' },
				targets: [
					{
						path: 'raw:///mnt/backup-nas/harbor/workstation/srv',
						status: 'ok',
						backup_count: 7,
						pending: 0
					}
				]
			}
		]
	},
	runtime: {
		profile_id: '11111111-1111-4111-8111-111111111111',
		timer_unit: 'btrfs-harbor-profile-11111111-1111-4111-8111-111111111111.timer',
		service_unit: 'btrfs-harbor-profile-11111111-1111-4111-8111-111111111111.service',
		timer_installed: true,
		timer_enabled: true,
		timer_active: true,
		timer_sub_state: 'waiting',
		next_elapse_realtime: 'Tomorrow · 02:00',
		last_trigger: 'Today · 02:00',
		service_active: false,
		service_sub_state: 'dead',
		service_result: 'success',
		service_exit_status: 0
	},
	destinationName: 'Backup NAS · NFS',
	destinationOnline: true,
	lastBackup: '02:13',
	lastVerify: '02:29',
	nextRun: 'Tomorrow · 02:00',
	transferred: '1.8 GiB',
	duration: '3m 42s',
	chainHealthy: true,
	restoreTested: true
};

export function protectionState(engine: EngineStatus): 'protected' | 'risk' | 'unprotected' {
	const targetLists = engine.volumes.map((volume) => volume.targets);
	const hasTarget = targetLists.some((targets) => targets.length > 0);
	const eachVolumeSynced =
		engine.volumes.length > 0 &&
		engine.volumes.every(
			(volume) =>
				volume.targets.length > 0 &&
				volume.targets.some(
					(target) => target.status === 'ok' && target.backup_count > 0 && target.pending === 0
				)
		);

	if (engine.healthy && hasTarget && eachVolumeSynced) return 'protected';
	if (!hasTarget || engine.volumes.length === 0) return 'unprotected';
	return 'risk';
}
