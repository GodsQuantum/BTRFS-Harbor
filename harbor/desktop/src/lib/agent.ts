import { Channel, invoke, isTauri } from '@tauri-apps/api/core';
import { open } from '@tauri-apps/plugin-dialog';
import { createDefaultConfiguration, type DiscoveredSource, type HarborConfig } from './config';
import {
	demoStatus,
	type BackupProgressEvent,
	type DashboardStatus,
	type EngineStatus,
	type ProfileRuntime
} from './status';

interface ProfileSummary {
	id: string;
	name: string;
	on_calendar: string;
	verify_after_backup: boolean;
}

export interface InstallationState {
	portable_appimage: boolean;
	helper_installed: boolean;
	service_unit_installed: boolean;
	service_available: boolean;
}

function integrationUnavailable(message: string): boolean {
	return (
		message.includes('Btrfs Harbor agent') ||
		message.includes('system D-Bus') ||
		message.includes('ServiceUnknown') ||
		message.includes('NameHasNoOwner')
	);
}

export async function loadInstallationState(): Promise<InstallationState> {
	if (!isTauri()) {
		return {
			portable_appimage: false,
			helper_installed: true,
			service_unit_installed: true,
			service_available: true
		};
	}
	const raw = await invoke<string>('installation_state');
	return JSON.parse(raw) as InstallationState;
}

export async function loadHarborConfiguration(): Promise<HarborConfig> {
	if (!isTauri()) {
		return createDefaultConfiguration();
	}

	try {
		const raw = await invoke<string>('configuration');
		return JSON.parse(raw) as HarborConfig;
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		if (
			message.includes('/etc/btrfs-harbor/harbor.toml') ||
			message.includes('FileNotFound') ||
			message.includes('No such file') ||
			integrationUnavailable(message)
		) {
			return createDefaultConfiguration();
		}
		throw error;
	}
}

export async function applyHarborConfiguration(
	config: HarborConfig,
	profileId: string
): Promise<string> {
	if (!isTauri()) {
		await new Promise((resolve) => setTimeout(resolve, 500));
		return 'demo';
	}

	const installation = await loadInstallationState();
	if (!installation.helper_installed) {
		throw new Error(
			'Install Harbor first to enable automatic backups. Portable mode can back up manually without installation.'
		);
	}

	return invoke<string>('apply_configuration', {
		configuration: JSON.stringify(config),
		profileId
	});
}

export async function chooseDestinationDirectory(defaultPath?: string): Promise<string | null> {
	if (!isTauri()) return null;
	const selected = await open({
		directory: true,
		multiple: false,
		defaultPath: defaultPath || undefined,
		title: 'Choose Btrfs Harbor backup destination'
	});
	return typeof selected === 'string' ? selected : null;
}

export interface SystemIdentity {
	hostname: string;
	pretty_name: string;
	architecture: string;
	root_fs: string;
}

export interface MountProbe {
	mount_point: string;
	source: string;
	fs_type: string;
	kind: 'nfs' | 'smb' | 'raw';
}

export async function loadSystemIdentity(): Promise<SystemIdentity> {
	if (!isTauri()) {
		return {
			hostname: 'workstation',
			pretty_name: 'Linux',
			architecture: 'x86_64',
			root_fs: 'btrfs'
		};
	}
	const raw = await invoke<string>('system_identity');
	return JSON.parse(raw) as SystemIdentity;
}

export async function installFullHarbor(): Promise<string> {
	if (!isTauri()) return 'demo';
	return invoke<string>('install_full_package');
}

export async function inspectDestinationMount(path: string): Promise<MountProbe | null> {
	if (!isTauri()) return null;
	const raw = await invoke<string>('inspect_mount', { path });
	return JSON.parse(raw) as MountProbe;
}

export async function discoverBtrfsSources(): Promise<DiscoveredSource[]> {
	if (!isTauri()) {
		return [
			{
				mount_point: '/',
				source: '/dev/mapper/root[/@]',
				subvolume: '@',
				snapper_config: 'root',
				hint: 'recommended'
			},
			{
				mount_point: '/home',
				source: '/dev/mapper/root[/@home]',
				subvolume: '@home',
				snapper_config: null,
				hint: 'recommended'
			},
			{
				mount_point: '/root',
				source: '/dev/mapper/root[/@root]',
				subvolume: '@root',
				snapper_config: null,
				hint: 'recommended'
			},
			{
				mount_point: '/srv',
				source: '/dev/mapper/root[/@srv]',
				subvolume: '@srv',
				snapper_config: null,
				hint: 'recommended'
			},
			{
				mount_point: '/var/cache',
				source: '/dev/mapper/root[/@cache]',
				subvolume: '@cache',
				snapper_config: null,
				hint: 'usually_disposable'
			}
		];
	}

	const raw = await invoke<string>('discover_sources');
	return JSON.parse(raw) as DiscoveredSource[];
}

export async function loadProfileRuntime(profileId: string): Promise<ProfileRuntime | null> {
	if (!isTauri()) return demoStatus.runtime ?? null;
	try {
		const raw = await invoke<string>('profile_runtime', { profileId });
		return JSON.parse(raw) as ProfileRuntime;
	} catch {
		return null;
	}
}

export async function loadDashboardStatus(): Promise<DashboardStatus> {
	if (!isTauri()) {
		return demoStatus;
	}

	const installation = await loadInstallationState().catch(() => null);

	try {
		const profilesRaw = await invoke<string>('profiles');
		const profiles = JSON.parse(profilesRaw) as ProfileSummary[];
		const active = profiles[0];
		if (!active) {
			throw new Error('No Btrfs Harbor profile is configured.');
		}

		const [statusRaw, runtime] = await Promise.all([
			invoke<string>('backup_status', { profileId: active.id }),
			loadProfileRuntime(active.id)
		]);
		const engine = JSON.parse(statusRaw) as EngineStatus;
		return {
			source: 'live',
			profileId: active.id,
			engine,
			runtime,
			destinationName: inferDestination(engine),
			destinationOnline: engine.volumes.some((volume) =>
				volume.targets.some((target) => !target.status.startsWith('error:'))
			),
			lastBackup: '—',
			lastVerify: '—',
			nextRun: runtime?.next_elapse_realtime ?? active.on_calendar,
			transferred: '—',
			duration: '—',
			chainHealthy: engine.healthy,
			restoreTested: false
		};
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		const unconfigured =
			message.includes('/etc/btrfs-harbor/harbor.toml') ||
			message.includes('No Btrfs Harbor profile is configured') ||
			message.includes('No such file');
		const setupRequired =
			unconfigured || integrationUnavailable(message) || installation?.service_available === false;

		return {
			source: setupRequired ? 'setup' : 'offline',
			engine: {
				schema_version: 1,
				healthy: false,
				volumes: [],
				error: unconfigured
					? null
					: {
							code: 'agent_unavailable',
							message
						}
			},
			runtime: null,
			destinationName: '—',
			destinationOnline: false,
			lastBackup: '—',
			lastVerify: '—',
			nextRun: '—',
			transferred: '—',
			duration: '—',
			chainHealthy: false,
			restoreTested: false,
			warning: setupRequired
				? installation?.portable_appimage
					? 'Portable mode: choose what to protect and a backup destination. Full installation is required only when you activate scheduled backups.'
					: 'Backup protection has not been configured on this computer yet.'
				: message
		};
	}
}

export async function runDraftBackup(
	config: HarborConfig,
	profileId: string,
	onProgress?: (event: BackupProgressEvent) => void
): Promise<void> {
	if (!isTauri()) {
		for (const event of [
			{ event: 'phase', phase: 'prepare', message: 'Loaded portable backup settings' },
			{ event: 'phase', phase: 'mount_guard', message: 'Demo destination is ready' },
			{ event: 'phase', phase: 'backup', message: 'Running portable backup' },
			{ event: 'finished', phase: 'complete', message: 'Portable backup complete' }
		] satisfies BackupProgressEvent[]) {
			onProgress?.(event);
			await new Promise((resolve) => setTimeout(resolve, 120));
		}
		return;
	}

	const channel = new Channel<BackupProgressEvent>();
	channel.onmessage = (event) => onProgress?.(event);
	await invoke<void>('send_draft_now_stream', {
		configuration: JSON.stringify(config),
		profileId,
		onEvent: channel
	});
}

export async function sendProfileNow(
	profileId: string,
	onProgress?: (event: BackupProgressEvent) => void
): Promise<void> {
	if (!isTauri()) {
		for (const event of [
			{ event: 'phase', phase: 'prepare', message: 'Loaded demo profile' },
			{ event: 'phase', phase: 'mount_guard', message: 'Demo destination is ready' },
			{ event: 'phase', phase: 'backup', message: 'Running demo backup' },
			{ event: 'phase', phase: 'verify', message: 'Verifying demo backup' },
			{ event: 'finished', phase: 'complete', message: 'Demo backup complete' }
		] satisfies BackupProgressEvent[]) {
			onProgress?.(event);
			await new Promise((resolve) => setTimeout(resolve, 120));
		}
		return;
	}

	const channel = new Channel<BackupProgressEvent>();
	channel.onmessage = (event) => onProgress?.(event);
	await invoke<void>('send_snapshot_now_stream', { profileId, onEvent: channel });
}

export interface RestorePoint {
	name: string;
	created: string | null;
	size: number | null;
	parent_name: string | null;
	checksum: string | null;
	origin: string | null;
}

export interface StagedRestoreRequest {
	destination_id: string;
	source_path: string;
	staging_root: string;
	snapshot: string | null;
	before: string | null;
}

export async function listRestorePoints(
	config: HarborConfig,
	profileId: string,
	sourcePath: string,
	destinationId: string
): Promise<RestorePoint[]> {
	if (!isTauri()) {
		return [
			{
				name: 'home-20261006T020000',
				created: '2026-10-06T02:00:00+00:00',
				size: 1_842_000_000,
				parent_name: 'home-20261005T020000',
				checksum: 'demo',
				origin: 'demo'
			},
			{
				name: 'home-20261005T020000',
				created: '2026-10-05T02:00:00+00:00',
				size: 1_790_000_000,
				parent_name: null,
				checksum: 'demo',
				origin: 'demo'
			}
		];
	}
	const raw = await invoke<string>('list_restore_points', {
		configuration: JSON.stringify(config),
		profileId,
		sourcePath,
		destinationId
	});
	return JSON.parse(raw) as RestorePoint[];
}

export async function chooseStagingDirectory(defaultPath?: string): Promise<string | null> {
	if (!isTauri()) return null;
	const selected = await open({
		directory: true,
		multiple: false,
		defaultPath: defaultPath || undefined,
		title: 'Choose Btrfs restore staging directory'
	});
	return typeof selected === 'string' ? selected : null;
}

export async function stageRestoreProfile(
	config: HarborConfig,
	profileId: string,
	request: StagedRestoreRequest,
	onProgress?: (event: BackupProgressEvent) => void
): Promise<void> {
	if (!isTauri()) {
		for (const event of [
			{ event: 'phase', phase: 'restore_start', message: 'Preparing demo restore' },
			{ event: 'phase', phase: 'restore_plan', message: 'Dry-running demo restore' },
			{ event: 'phase', phase: 'restore', message: 'Receiving into demo staging' },
			{ event: 'phase', phase: 'restore_verify', message: 'Verifying staged demo subvolume' },
			{ event: 'finished', phase: 'restore_complete', message: 'Demo staged restore ready' }
		] satisfies BackupProgressEvent[]) {
			onProgress?.(event);
			await new Promise((resolve) => setTimeout(resolve, 120));
		}
		return;
	}

	const channel = new Channel<BackupProgressEvent>();
	channel.onmessage = (event) => onProgress?.(event);
	await invoke<void>('stage_restore', {
		configuration: JSON.stringify(config),
		profileId,
		request: JSON.stringify(request),
		onEvent: channel
	});
}

export interface LxcReplicaRequest {
	vmid: number;
	confirm_vmid: number;
	staging_path: string;
	target_name: string;
	create_rollback_snapshot: boolean;
	start_after: boolean;
	regenerate_ssh_host_keys: boolean;
}

export async function replicateLxc(
	request: LxcReplicaRequest,
	onProgress?: (event: BackupProgressEvent) => void
): Promise<void> {
	if (!isTauri()) {
		for (const event of [
			{ event: 'phase', phase: 'replica_preflight', message: 'Checking stopped demo LXC' },
			{ event: 'phase', phase: 'replica_snapshot', message: 'Creating demo rollback snapshot' },
			{ event: 'phase', phase: 'replica_mount', message: 'Mounting demo rootfs' },
			{ event: 'phase', phase: 'replica_copy', message: 'Transplanting demo userspace' },
			{ event: 'finished', phase: 'replica_complete', message: 'Demo LXC replica complete' }
		] satisfies BackupProgressEvent[]) {
			onProgress?.(event);
			await new Promise((resolve) => setTimeout(resolve, 140));
		}
		return;
	}

	const channel = new Channel<BackupProgressEvent>();
	channel.onmessage = (event) => onProgress?.(event);
	await invoke<void>('replicate_lxc', {
		request: JSON.stringify(request),
		onEvent: channel
	});
}

export async function installProfile(profileId: string): Promise<string> {
	if (!isTauri()) return 'demo';
	return invoke<string>('install_profile', { profileId });
}

export async function uninstallProfile(profileId: string): Promise<string> {
	if (!isTauri()) return 'demo';
	return invoke<string>('uninstall_profile', { profileId });
}

function inferDestination(engine: EngineStatus): string {
	const target = engine.volumes.flatMap((volume) => volume.targets)[0];
	if (!target) return '—';
	return target.path.replace(/^raw:\/\//, '');
}
