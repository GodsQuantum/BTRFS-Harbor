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
			message.includes('No such file')
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

export interface MountProbe {
	mount_point: string;
	source: string;
	fs_type: string;
	kind: 'nfs' | 'smb' | 'raw';
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

		return {
			source: unconfigured ? 'live' : 'offline',
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
			warning: unconfigured ? 'No Btrfs Harbor profile is configured.' : message
		};
	}
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

export interface StagedRestoreRequest {
	destination_id: string;
	source_path: string;
	staging_root: string;
	before: string | null;
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
