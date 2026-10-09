<script lang="ts">
	import { onMount } from 'svelte';
	import Activity from 'lucide-svelte/icons/activity';
	import Anchor from 'lucide-svelte/icons/anchor';
	import ArchiveRestore from 'lucide-svelte/icons/archive-restore';
	import ArrowRight from 'lucide-svelte/icons/arrow-right';
	import Box from 'lucide-svelte/icons/box';
	import Check from 'lucide-svelte/icons/check';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import CircleAlert from 'lucide-svelte/icons/circle-alert';
	import Clock3 from 'lucide-svelte/icons/clock-3';
	import Copy from 'lucide-svelte/icons/copy';
	import DatabaseBackup from 'lucide-svelte/icons/database-backup';
	import Gauge from 'lucide-svelte/icons/gauge';
	import HardDrive from 'lucide-svelte/icons/hard-drive';
	import History from 'lucide-svelte/icons/history';
	import Languages from 'lucide-svelte/icons/languages';
	import Laptop from 'lucide-svelte/icons/laptop';
	import Layers3 from 'lucide-svelte/icons/layers-3';
	import LifeBuoy from 'lucide-svelte/icons/life-buoy';
	import MonitorCog from 'lucide-svelte/icons/monitor-cog';
	import Moon from 'lucide-svelte/icons/moon';
	import Network from 'lucide-svelte/icons/network';
	import Plus from 'lucide-svelte/icons/plus';
	import Pencil from 'lucide-svelte/icons/pencil';
	import Server from 'lucide-svelte/icons/server';
	import Settings from 'lucide-svelte/icons/settings';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import ShieldEllipsis from 'lucide-svelte/icons/shield-ellipsis';
	import Sun from 'lucide-svelte/icons/sun';
	import TerminalSquare from 'lucide-svelte/icons/terminal-square';
	import Wifi from 'lucide-svelte/icons/wifi';
	import WifiOff from 'lucide-svelte/icons/wifi-off';
	import {
		applyHarborConfiguration,
		loadDashboardStatus,
		loadEngineSelectionStatus,
		loadEngineUpdateOptions,
		loadHarborConfiguration,
		loadInstallationState,
		loadProfileRuntime,
		loadSystemIdentity,
		openHarborReleasePage,
		persistEnginePolicy,
		updateActiveSystemEngine,
		type InstallationState,
		type SystemIdentity
	} from '#lib/agent.ts';
	import {
		appendDefaultBackupJob,
		cloneConfiguration,
		resolveDestination,
		resolveProfile,
		sourceDisplayName,
		type BackupProfile,
		type HarborConfig
	} from '#lib/config.ts';
	import {
		dictionaries,
		localeLabels,
		translate,
		type Locale,
		type TranslationKey
	} from '#lib/i18n.ts';
	import CheckpointTimeline from '#lib/CheckpointTimeline.svelte';
	import CheckpointControls from '#lib/CheckpointControls.svelte';
	import EngineManager from '#lib/EngineManager.svelte';
	import EngineStatusRow from '#lib/EngineStatusRow.svelte';
	import ProtectionEditor from '#lib/ProtectionEditor.svelte';
	import RecoveryEditor from '#lib/RecoveryEditor.svelte';
	import ReplicaEditor from '#lib/ReplicaEditor.svelte';
	import type { EnginePolicy, EngineSelectionStatus, EngineUpdateOptions } from '#lib/engine.ts';
	import {
		demoStatus,
		protectionState,
		runtimeRepresentsScheduledJob,
		type DashboardStatus,
		type ProfileRuntime
	} from '#lib/status.ts';

	type Page =
		| 'overview'
		| 'protection'
		| 'timeline'
		| 'recover'
		| 'replicate'
		| 'destinations'
		| 'activity'
		| 'settings';

	let locale: Locale = 'en';
	let active: Page = 'overview';
	let dark = false;
	let dashboard: DashboardStatus = demoStatus;
	let harborConfig: HarborConfig | null = null;
	let configWarning = '';
	let installationState: InstallationState | null = null;
	let systemIdentity: SystemIdentity | null = null;
	let engineStatus: EngineSelectionStatus | null = null;
	let engineUpdateOptions: EngineUpdateOptions | null = null;
	let engineBusy = false;
	let engineError = '';
	let scheduleSaving = false;
	let scheduleFeedback = '';
	let scheduleError = '';
	let editingJobId: string | null = null;
	let jobRuntimes: Record<string, ProfileRuntime | null> = {};
	$: protection = protectionState(dashboard.engine);
	$: activeProfile = harborConfig?.profiles[0] ?? null;
	$: activeSchedule = activeProfile?.on_calendar ?? '';
	$: activeProfileHasSnapper =
		activeProfile?.sources.some((source) => Boolean(source.snapper_config)) ?? false;
	$: scheduledJobs =
		harborConfig?.profiles.filter(
			(profile) =>
				runtimeRepresentsScheduledJob(jobRuntimes[profile.id]) || profile.id === editingJobId
		) ?? [];
	const t = (key: TranslationKey) => translate(locale, key);

	onMount(async () => {
		const savedLocale = localStorage.getItem('btrfs-harbor-locale') as Locale | null;
		if (savedLocale && savedLocale in dictionaries) locale = savedLocale;
		const savedTheme = localStorage.getItem('btrfs-harbor-theme');
		dark = savedTheme
			? savedTheme === 'dark'
			: window.matchMedia('(prefers-color-scheme: dark)').matches;
		const savedEnginePolicy = localStorage.getItem(
			'btrfs-harbor-engine-policy'
		) as EnginePolicy | null;

		const [dashboardResult, installState, identity] = await Promise.all([
			loadDashboardStatus(),
			loadInstallationState().catch(() => null),
			loadSystemIdentity().catch(() => null)
		]);
		dashboard = dashboardResult;
		installationState = installState;
		systemIdentity = identity;
		try {
			harborConfig = await loadHarborConfiguration();
			if (
				!installState?.helper_installed &&
				savedEnginePolicy &&
				['auto', 'system', 'bundled'].includes(savedEnginePolicy)
			) {
				harborConfig.engine_policy = savedEnginePolicy;
			}
			await Promise.all([
				refreshJobRuntimes(harborConfig, installState),
				refreshEngineState(harborConfig.engine_policy)
			]);
		} catch (error) {
			configWarning = error instanceof Error ? error.message : String(error);
			await refreshEngineState(savedEnginePolicy ?? 'auto').catch(() => undefined);
		}
	});

	function setLocale(next: Locale) {
		locale = next;
		localStorage.setItem('btrfs-harbor-locale', next);
	}

	function toggleTheme() {
		dark = !dark;
		localStorage.setItem('btrfs-harbor-theme', dark ? 'dark' : 'light');
	}

	async function refreshEngineState(policy: EnginePolicy = harborConfig?.engine_policy ?? 'auto') {
		engineError = '';
		try {
			const status = await loadEngineSelectionStatus(policy);
			engineStatus = status;
			engineUpdateOptions = await loadEngineUpdateOptions(policy);
		} catch (error) {
			engineStatus = null;
			engineUpdateOptions = null;
			engineError = error instanceof Error ? error.message : String(error);
			throw error;
		}
	}

	async function changeEnginePolicy(policy: EnginePolicy) {
		if (!harborConfig || engineBusy) return;
		engineBusy = true;
		engineError = '';
		const previous = harborConfig.engine_policy;
		const next = cloneConfiguration(harborConfig);
		next.engine_policy = policy;
		harborConfig = next;
		localStorage.setItem('btrfs-harbor-engine-policy', policy);
		try {
			if (installationState?.helper_installed) {
				await persistEnginePolicy(policy);
			}
			await refreshEngineState(policy);
		} catch (error) {
			engineError = error instanceof Error ? error.message : String(error);
			const rollback = cloneConfiguration(harborConfig);
			rollback.engine_policy = previous;
			harborConfig = rollback;
			localStorage.setItem('btrfs-harbor-engine-policy', previous);
		} finally {
			engineBusy = false;
		}
	}

	async function updateEngine() {
		if (!engineStatus || !engineUpdateOptions || engineBusy) return;
		engineBusy = true;
		engineError = '';
		try {
			if (engineUpdateOptions.strategy === 'harbor_application') {
				await openHarborReleasePage();
				return;
			}
			if (!engineUpdateOptions.can_update) return;
			engineStatus = await updateActiveSystemEngine(harborConfig?.engine_policy ?? 'auto');
			engineUpdateOptions = await loadEngineUpdateOptions(harborConfig?.engine_policy ?? 'auto');
		} catch (error) {
			engineError = error instanceof Error ? error.message : String(error);
		} finally {
			engineBusy = false;
		}
	}

	async function refreshEngineFromUi() {
		if (engineBusy) return;
		engineBusy = true;
		try {
			await refreshEngineState();
		} catch {
			// refreshEngineState already exposes the actionable error in the UI.
		} finally {
			engineBusy = false;
		}
	}

	const overviewSchedules = [
		['hourly', 'hourlySchedule'],
		['*-*-* 02:00:00', 'dailySchedule'],
		['Sun *-*-* 02:00:00', 'weeklySchedule'],
		['@snapshots', 'snapshotSchedule']
	] as const;

	function readableSchedule(value: string): string {
		const found = overviewSchedules.find(([spec]) => spec === value);
		return found ? t(found[1]) : value;
	}

	async function updateOverviewSchedule(spec: string) {
		if (!harborConfig || !activeProfile) return;
		scheduleSaving = true;
		scheduleFeedback = '';
		scheduleError = '';
		try {
			const next = cloneConfiguration(harborConfig);
			const profile = resolveProfile(next, activeProfile.id);
			profile.on_calendar = spec;
			await applyHarborConfiguration(next, profile.id);
			harborConfig = next;
			dashboard = await loadDashboardStatus();
			scheduleFeedback = t('scheduleUpdated');
		} catch (error) {
			scheduleError = error instanceof Error ? error.message : String(error);
		} finally {
			scheduleSaving = false;
		}
	}

	function pageTitle(page: Page): string {
		if (page === 'protection') return t('scheduledJobs');
		return t(page as TranslationKey);
	}

	async function refreshJobRuntimes(
		config: HarborConfig | null = harborConfig,
		installation: InstallationState | null = installationState
	) {
		if (!config || !installation?.helper_installed) {
			jobRuntimes = {};
			return;
		}
		const entries = await Promise.all(
			config.profiles.map(
				async (profile) => [profile.id, await loadProfileRuntime(profile.id)] as const
			)
		);
		jobRuntimes = Object.fromEntries(entries);
	}

	function addBackupJob() {
		if (!harborConfig || !installationState?.helper_installed) return;
		const next = appendDefaultBackupJob(harborConfig);
		harborConfig = next;
		editingJobId = next.profiles.at(-1)?.id ?? null;
	}

	function jobDestination(profile: BackupProfile): string {
		if (!harborConfig) return t('notConfigured');
		try {
			const destination = resolveDestination(harborConfig, profile);
			return destination.path.trim() || destination.name || t('notConfigured');
		} catch {
			return t('notConfigured');
		}
	}

	function jobSources(profile: BackupProfile): string {
		if (profile.sources.length === 0) return t('notConfigured');
		return profile.sources.map((source) => sourceDisplayName(source.path)).join(', ');
	}

	const timelineRows = [
		{
			time: '02:13',
			name: 'srv-20261005T021300',
			source: '/srv',
			tags: ['local', 'backedUp', 'verified', 'incremental']
		},
		{
			time: '02:12',
			name: 'root-home-20261005T021200',
			source: '/root',
			tags: ['local', 'backedUp', 'verified', 'incremental']
		},
		{
			time: '02:11',
			name: 'home-20261005T021100',
			source: '/home',
			tags: ['local', 'backedUp', 'verified', 'incremental']
		},
		{
			time: '02:10',
			name: 'root-20261005T021000',
			source: '/',
			tags: ['local', 'backedUp', 'verified', 'incremental', 'restoreTested']
		}
	] as const;
</script>

<div class:dark class="app-shell">
	<aside class="sidebar">
		<button class="brand" onclick={() => (active = 'overview')} aria-label={t('overview')}>
			<img src="/logo-mark.svg" alt="" />
			<div>
				<strong>{t('appName')}</strong>
				<span>{t('tagline')}</span>
			</div>
		</button>

		<nav aria-label="Primary">
			<button class:active={active === 'overview'} onclick={() => (active = 'overview')}>
				<Gauge size={18} strokeWidth={1.8} /><span>{t('overview')}</span>
			</button>
			<button class:active={active === 'protection'} onclick={() => (active = 'protection')}>
				<Clock3 size={18} strokeWidth={1.8} /><span>{t('scheduledJobs')}</span>
			</button>
			{#if dashboard.source === 'live' || dashboard.source === 'demo'}
				<button class:active={active === 'timeline'} onclick={() => (active = 'timeline')}>
					<History size={18} strokeWidth={1.8} /><span>{t('timeline')}</span>
				</button>
			{/if}
			{#if dashboard.source !== 'offline'}
				<button class:active={active === 'recover'} onclick={() => (active = 'recover')}>
					<LifeBuoy size={18} strokeWidth={1.8} /><span>{t('recover')}</span>
				</button>
			{/if}
			<button class:active={active === 'settings'} onclick={() => (active = 'settings')}>
				<Settings size={18} strokeWidth={1.8} /><span>{t('settings')}</span>
			</button>
		</nav>

		<div class="sidebar-foot">
			<div class="agent-state">
				<span
					class:live={dashboard.source === 'live'}
					class:demo={dashboard.source === 'demo' || dashboard.source === 'setup'}
					class:offline={dashboard.source === 'offline'}
				></span>
				<div>
					<strong>
						{dashboard.source === 'live'
							? t('protectionActive')
							: dashboard.source === 'demo'
								? t('demoData')
								: dashboard.source === 'setup'
									? installationState?.portable_appimage
										? t('portableMode')
										: t('setupRequired')
									: t('systemProblem')}
					</strong>
					<small>
						{dashboard.source === 'live'
							? t('backgroundProtection')
							: dashboard.source === 'setup'
								? t('setupThisComputer')
								: t('systemStatus')}
					</small>
				</div>
			</div>
			{#if dashboard.source === 'live'}<p>{t('closeSafe')}</p>{/if}
		</div>
	</aside>

	<main>
		<header class="topbar">
			<div>
				<p class="eyebrow">{t('comingFoundation')}</p>
				<h1>{pageTitle(active)}</h1>
			</div>
			<div class="top-actions">
				<div class="locale">
					<Languages size={16} />
					<select
						aria-label={t('language')}
						value={locale}
						onchange={(event) =>
							setLocale((event.currentTarget as HTMLSelectElement).value as Locale)}
					>
						{#each Object.entries(localeLabels) as [code, label] (code)}
							<option value={code}>{label}</option>
						{/each}
					</select>
				</div>
				<button class="icon-button" onclick={toggleTheme} aria-label={t('theme')}>
					{#if dark}<Sun size={17} />{:else}<Moon size={17} />{/if}
				</button>
			</div>
		</header>

		{#if dashboard.source === 'demo'}
			<div class="demo-banner">
				<DatabaseBackup size={15} />
				<div>
					<strong>{t('demoData')}</strong>
					<span>{t('demoExplain')}</span>
				</div>
			</div>
		{:else if dashboard.source === 'offline'}
			<div class="demo-banner danger">
				<CircleAlert size={16} />
				<div>
					<strong>{t('systemProblem')}</strong>
					<span>{dashboard.warning ?? t('noLiveProfile')}</span>
				</div>
			</div>
		{/if}

		{#if active === 'overview' && dashboard.source === 'setup'}
			<section class="content-stack setup-stack">
				<div class="page-intro">
					<div class="intro-icon"><Laptop size={24} /></div>
					<div>
						<h2>{t('setupTitle')}</h2>
						<p>{t('setupIntro')}</p>
						{#if systemIdentity}
							<div class="system-facts">
								<span><strong>{systemIdentity.hostname}</strong></span>
								<span>{systemIdentity.pretty_name}</span>
								<span>{systemIdentity.architecture}</span>
								<span class:good={systemIdentity.root_fs === 'btrfs'}>{systemIdentity.root_fs}</span
								>
							</div>
						{/if}
					</div>
				</div>
				<EngineStatusRow
					status={engineStatus}
					{locale}
					loading={engineBusy}
					onManage={() => (active = 'settings')}
				/>
				{#if installationState?.portable_appimage && !installationState.helper_installed}
					<article class="callout install-callout">
						<CircleAlert size={20} />
						<div>
							<strong>{t('portableMode')}</strong>
							<p>{t('portableModeDesc')}</p>
						</div>
					</article>
				{/if}
				{#if harborConfig}
					<ProtectionEditor
						bind:config={harborConfig}
						{locale}
						runtime={null}
						onRecover={() => {
							active = 'recover';
						}}
						onApplied={async () => {
							dashboard = await loadDashboardStatus();
							installationState = await loadInstallationState().catch(() => installationState);
							harborConfig = await loadHarborConfiguration();
							await refreshJobRuntimes(harborConfig, installationState);
						}}
					/>
				{:else}
					<article class="callout danger">
						<CircleAlert size={21} />
						<div>
							<strong>{t('systemProblem')}</strong>
							<p>{configWarning || t('setupIntro')}</p>
						</div>
					</article>
				{/if}
			</section>
		{:else if active === 'overview'}
			<section class="overview-grid">
				<EngineStatusRow
					status={engineStatus}
					{locale}
					loading={engineBusy}
					onManage={() => (active = 'settings')}
				/>
				<article class="protection-hero {protection}">
					<div class="hero-copy">
						<div class="status-icon">
							{#if protection === 'protected'}
								<ShieldCheck size={28} />
							{:else if protection === 'risk'}
								<ShieldEllipsis size={28} />
							{:else}
								<CircleAlert size={28} />
							{/if}
						</div>
						<div>
							<p class="eyebrow">{t('protection')}</p>
							<h2>
								{protection === 'protected'
									? t('systemProtected')
									: protection === 'risk'
										? t('systemAtRisk')
										: t('systemUnprotected')}
							</h2>
							<p>
								{protection === 'protected'
									? t('protectedDescription')
									: protection === 'risk'
										? t('atRiskDescription')
										: t('unprotectedDescription')}
							</p>
						</div>
					</div>
					<button class="secondary" onclick={() => (active = 'recover')}>
						<LifeBuoy size={18} />
						{t('recoverFromSnapshot')}
					</button>
				</article>

				{#if harborConfig && activeProfile && activeProfileHasSnapper}
					<div style="grid-column: 1 / -1; min-width: 0">
						<CheckpointControls
							profile={activeProfile}
							destination={resolveDestination(harborConfig, activeProfile)}
							{locale}
						/>
					</div>
				{/if}

				<details class="overview-extra" style="grid-column: 1 / -1; min-width: 0">
					<summary style="cursor: pointer; font-size: 14px; font-weight: 700; padding: 12px 4px">
						{locale === 'fr'
							? 'Planification, volumes et diagnostic'
							: locale === 'zh-CN'
								? '计划、卷和诊断'
								: 'Schedules, volumes and diagnostics'}
					</summary>
					<div class="overview-grid">
						{#if dashboard.source === 'live' && harborConfig && activeProfile}
							<article class="panel overview-schedule">
								<div class="overview-schedule-copy">
									<span>{t('schedule')}</span>
									<strong>{readableSchedule(activeSchedule)}</strong>
									<small>{t('simpleScheduleHelp')}</small>
								</div>
								<div class="schedule-options compact-options">
									{#each overviewSchedules as [spec, key] (spec)}
										<button
											type="button"
											class:active={activeSchedule === spec}
											disabled={scheduleSaving ||
												(spec === '@snapshots' && !activeProfileHasSnapper)}
											title={spec === '@snapshots' && !activeProfileHasSnapper
												? t('snapshotScheduleNeedsSnapper')
												: ''}
											onclick={() => updateOverviewSchedule(spec)}
										>
											{t(key)}
										</button>
									{/each}
								</div>
								{#if scheduleFeedback}<small class="good-status">{scheduleFeedback}</small>{/if}
								{#if scheduleError}<small class="error-text">{scheduleError}</small>{/if}
							</article>
						{/if}

						<div class="metric-grid">
							<article class="metric">
								<div class="metric-icon"><DatabaseBackup size={19} /></div>
								<div>
									<span>{t('lastOffHostBackup')}</span>
									<strong>{dashboard.lastBackup}</strong>
									<small><Check size={13} /> {t('verified')}</small>
								</div>
							</article>
							<article class="metric">
								<div class="metric-icon"><ShieldCheck size={19} /></div>
								<div>
									<span>{t('lastVerification')}</span>
									<strong>{dashboard.lastVerify}</strong>
									<small><Check size={13} /> SHA-256</small>
								</div>
							</article>
							<article class="metric">
								<div class="metric-icon"><Clock3 size={19} /></div>
								<div>
									<span>{t('nextRun')}</span>
									<strong
										>{activeSchedule === '@snapshots'
											? t('snapshotSchedule')
											: dashboard.nextRun}</strong
									>
									<small>{t('backgroundProtection')}</small>
								</div>
							</article>
							<article class="metric">
								<div class="metric-icon"><Network size={19} /></div>
								<div>
									<span>{t('destination')}</span>
									<strong>{dashboard.destinationName}</strong>
									<small class:warn={!dashboard.destinationOnline}>
										{#if dashboard.destinationOnline}<Wifi size={13} />{:else}<WifiOff
												size={13}
											/>{/if}
										{dashboard.destinationOnline ? t('online') : t('offline')}
									</small>
								</div>
							</article>
						</div>

						<article class="panel volumes-panel">
							<div class="panel-head">
								<div>
									<p class="eyebrow">{t('volumesProtected')}</p>
									<h3>{dashboard.engine.volumes.length}</h3>
								</div>
								<div class="chain-pill">
									<Layers3 size={15} />
									<span>{t('chainHealth')}</span>
									<strong>{dashboard.chainHealthy ? t('healthy') : t('attention')}</strong>
								</div>
							</div>
							<div class="volume-table">
								<div class="table-header">
									<span>{t('source')}</span>
									<span>{t('latestSnapshot')}</span>
									<span>{t('sourceSnapshots')}</span>
									<span>{t('remoteBackups')}</span>
									<span>{t('status')}</span>
								</div>
								{#each dashboard.engine.volumes as volume (volume.path)}
									<div class="table-row">
										<strong>{volume.path}</strong>
										<code>{volume.source.latest_snapshot ?? '—'}</code>
										<span>{volume.source.snapshot_count}</span>
										<span>{volume.targets[0]?.backup_count ?? 0}</span>
										<span class="good-status"><Check size={14} /> {t('backedUp')}</span>
									</div>
								{/each}
							</div>
						</article>

						<article class="panel activity-panel">
							<div class="panel-head">
								<div>
									<p class="eyebrow">{t('recentActivity')}</p>
									<h3>{t('activity')}</h3>
								</div>
								<button class="text-button" onclick={() => (active = 'activity')}>
									{t('viewAll')}
									<ChevronRight size={15} />
								</button>
							</div>
							<div class="activity-list">
								{#if dashboard.source === 'demo'}
									<div>
										<span class="event-dot ok"><Check size={12} /></span>
										<p>
											<strong>{t('backupCompleted')}</strong><small
												>02:13 · {dashboard.transferred} · {dashboard.duration}</small
											>
										</p>
										<span>{t('incremental')}</span>
									</div>
									<div>
										<span class="event-dot ok"><ShieldCheck size={12} /></span>
										<p>
											<strong>{t('verificationCompleted')}</strong><small
												>02:29 · 4 streams · SHA-256</small
											>
										</p>
										<span>{t('verified')}</span>
									</div>
									<div>
										<span class="event-dot neutral"><Clock3 size={12} /></span>
										<p>
											<strong>{t('scheduleCreated')}</strong><small>Yesterday · systemd timer</small
											>
										</p>
										<span>{t('everyDay')}</span>
									</div>
								{:else}
									<p class="empty-state">{t('noActivityYet')}</p>
								{/if}
							</div>
						</article>
					</div>
				</details>
			</section>
		{:else if active === 'protection'}
			<section class="content-stack scheduled-jobs-page">
				<div class="page-intro jobs-intro">
					<div class="intro-icon"><Clock3 size={24} /></div>
					<div>
						<h2>{t('scheduledJobs')}</h2>
						<p>{t('scheduledJobsIntro')}</p>
					</div>
					{#if harborConfig && installationState?.helper_installed}
						<button class="primary compact" type="button" onclick={addBackupJob}>
							<Plus size={16} />
							{t('addBackupJob')}
						</button>
					{/if}
				</div>

				{#if !installationState?.helper_installed}
					<article class="panel empty-panel scheduled-empty">
						<Clock3 size={24} />
						<div>
							<h3>{t('noScheduledJobs')}</h3>
							<p>{t('noScheduledJobsDesc')}</p>
						</div>
						<button class="primary" type="button" onclick={() => (active = 'overview')}>
							{t('installAutomation')}
						</button>
					</article>
				{:else if harborConfig}
					<div class="scheduled-jobs-grid">
						{#each scheduledJobs as job (job.id)}
							<article class:editing={editingJobId === job.id} class="panel scheduled-job-card">
								<div class="scheduled-job-head">
									<div>
										<span class="eyebrow">{t('profile')}</span>
										<h3>{job.name}</h3>
									</div>
									<button
										class="secondary compact"
										type="button"
										onclick={() => (editingJobId = editingJobId === job.id ? null : job.id)}
									>
										<Pencil size={14} />
										{editingJobId === job.id ? t('doneEditing') : t('editJob')}
									</button>
								</div>
								<div class="scheduled-job-summary">
									<div><span>{t('source')}</span><strong>{jobSources(job)}</strong></div>
									<div><span>{t('destination')}</span><code>{jobDestination(job)}</code></div>
									<div>
										<span>{t('schedule')}</span><strong>{readableSchedule(job.on_calendar)}</strong>
										<small class:good-status={jobRuntimes[job.id]?.timer_active}>
											{jobRuntimes[job.id]?.timer_active ? t('timerActive') : t('timerDisabled')}
										</small>
									</div>
								</div>
							</article>
						{/each}
					</div>
					{#if scheduledJobs.length === 0 && !editingJobId}
						<article class="panel empty-panel scheduled-empty">
							<Clock3 size={22} />
							<p>{t('noScheduledJobs')}</p>
						</article>
					{/if}

					{#if editingJobId}
						{#key editingJobId}
							<div class="scheduled-job-editor">
								<ProtectionEditor
									bind:config={harborConfig}
									profileId={editingJobId}
									{locale}
									runtime={jobRuntimes[editingJobId] ?? null}
									onRecover={() => {
										active = 'recover';
									}}
									onApplied={async () => {
										dashboard = await loadDashboardStatus();
										harborConfig = await loadHarborConfiguration();
										installationState = await loadInstallationState().catch(
											() => installationState
										);
										await refreshJobRuntimes(harborConfig, installationState);
									}}
								/>
							</div>
						{/key}
					{/if}
				{:else}
					<article class="callout danger">
						<CircleAlert size={21} />
						<div>
							<strong>{t('systemProblem')}</strong>
							<p>{configWarning || t('noLiveProfile')}</p>
						</div>
					</article>
				{/if}
			</section>
		{:else if active === 'timeline'}
			<section class="content-stack">
				<CheckpointTimeline
					target={harborConfig && activeProfile
						? resolveDestination(harborConfig, activeProfile).path
						: ''}
					{locale}
				/>
				<div class="page-intro">
					<div class="intro-icon"><History size={24} /></div>
					<div>
						<h2>{t('timelineTitle')}</h2>
						<p>{t('timelineIntro')}</p>
					</div>
				</div>
				<article class="panel timeline">
					{#if dashboard.source === 'demo'}
						{#each timelineRows as row, index (row.name)}
							<div class="timeline-row">
								<div class="timeline-time">{row.time}<span></span></div>
								<div class="timeline-main">
									<div><strong>{row.source}</strong><code>{row.name}</code></div>
									<div class="badges">
										{#each row.tags as tag (tag)}
											<span
												class:good={tag === 'backedUp' ||
													tag === 'verified' ||
													tag === 'restoreTested'}>{t(tag)}</span
											>
										{/each}
									</div>
								</div>
								<div class="timeline-chain">
									{index === timelineRows.length - 1 ? t('full') : t('incremental')}
								</div>
							</div>
						{/each}
					{:else}
						<p class="empty-state">{t('noActivityYet')}</p>
					{/if}
				</article>
			</section>
		{:else if active === 'recover'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><LifeBuoy size={24} /></div>
					<div>
						<h2>{t('recoveryTitle')}</h2>
						<p>{t('recoveryIntro')}</p>
					</div>
				</div>
				<div class="choice-grid">
					<article class="choice">
						<ArchiveRestore size={25} />
						<h3>{t('recoverFile')}</h3>
						<p>{t('stagedRestoreDesc')}</p>
						<ArrowRight size={18} />
					</article>
					<article class="choice">
						<Layers3 size={25} />
						<h3>{t('recoverSubvolume')}</h3>
						<p>{t('stagedRestoreDesc')}</p>
						<ArrowRight size={18} />
					</article>
					<article class="choice emphasis">
						<MonitorCog size={25} />
						<h3>{t('recoverSystem')}</h3>
						<p>{t('requiresRescue')}</p>
						<ArrowRight size={18} />
					</article>
				</div>
				{#if harborConfig}
					<RecoveryEditor config={harborConfig} profileId={dashboard.profileId} {locale} />
				{:else}
					<article class="callout large">
						<CircleAlert size={22} />
						<div>
							<strong>{t('systemProblem')}</strong>
							<p>{configWarning || t('noLiveProfile')}</p>
						</div>
					</article>
				{/if}
			</section>
		{:else if active === 'replicate'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><Copy size={24} /></div>
					<div>
						<h2>{t('replicaTitle')}</h2>
						<p>{t('replicaIntro')}</p>
					</div>
				</div>
				<div class="choice-grid">
					<article class="choice">
						<Laptop size={25} />
						<h3>{t('bareMetal')}</h3>
						<p>Userspace + boot reconciliation</p>
						<ArrowRight size={18} />
					</article>
					<article class="choice">
						<Server size={25} />
						<h3>{t('virtualMachine')}</h3>
						<p>Userspace + virtual hardware reconciliation</p>
						<ArrowRight size={18} />
					</article>
					<article class="choice emphasis">
						<Box size={25} />
						<h3>{t('lxcContainer')}</h3>
						<p>{t('lxcNote')}</p>
						<ArrowRight size={18} />
					</article>
				</div>
				<article class="callout large">
					<Layers3 size={22} />
					<div>
						<strong>{t('btrfsStaging')}</strong>
						<p>{t('btrfsStagingDesc')}</p>
					</div>
				</article>
				<ReplicaEditor advanced={false} {locale} />
			</section>
		{:else if active === 'destinations'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><HardDrive size={24} /></div>
					<div>
						<h2>{t('destinationTitle')}</h2>
						<p>{t('anyFilesystem')}</p>
					</div>
					<button class="primary compact" onclick={() => (active = 'protection')}
						>+ {t('addDestination')}</button
					>
				</div>
				{#if harborConfig?.destinations.some((destination) => destination.path.trim())}
					{#each harborConfig.destinations.filter( (destination) => destination.path.trim() ) as destination (destination.id)}
						<article class="panel destination-card">
							<div class="destination-symbol">
								{#if destination.kind === 'nfs' || destination.kind === 'smb'}
									<Network size={22} />
								{:else}
									<HardDrive size={22} />
								{/if}
							</div>
							<div class="destination-copy">
								<strong>{destination.name}</strong>
								<span
									>{destination.kind === 'ssh' ? t('sshDestination') : t('folderDestination')}</span
								>
								<code>{destination.path}</code>
							</div>
						</article>
					{/each}
				{:else}
					<article class="panel empty-panel">
						<HardDrive size={22} />
						<p>{t('noDestinations')}</p>
						<button class="secondary" onclick={() => (active = 'protection')}>
							{t('configureProtection')}
						</button>
					</article>
				{/if}
				<article class="callout">
					<CircleAlert size={20} />
					<div>
						<strong>{t('noFallback')}</strong>
						<p>{t('mountGuardDesc')}</p>
					</div>
				</article>
			</section>
		{:else if active === 'activity'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><Activity size={24} /></div>
					<div>
						<h2>{t('activityTitle')}</h2>
						<p>{t('eventLog')} · JSONL</p>
					</div>
				</div>
				<article class="panel log-panel">
					{#if dashboard.source === 'demo'}
						{#each [['02:29:18', 'verify', 'completed', '4 streams · SHA-256'], ['02:13:44', 'backup', 'completed', '1.8 GiB · 3m 42s'], ['02:10:02', 'snapshot', 'completed', '@ · @home · @root · @srv'], ['Yesterday', 'schedule', 'created', 'OnCalendar=*-*-* 02:00']] as event (event[0] + event[1])}
							<div class="log-row">
								<code>{event[0]}</code><strong>{event[1]}</strong><span class="badge good"
									>{event[2]}</span
								><span>{event[3]}</span>
							</div>
						{/each}
					{:else}
						<p class="empty-state">{t('noActivityYet')}</p>
					{/if}
				</article>
			</section>
		{:else}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><Settings size={24} /></div>
					<div>
						<h2>{t('settingsTitle')}</h2>
						<p>{t('footerNative')}</p>
					</div>
				</div>
				<div class="two-column">
					<article class="panel">
						<div class="panel-head">
							<div>
								<p class="eyebrow">{t('interface')}</p>
								<h3>{t('appName')}</h3>
							</div>
						</div>
						<div class="setting-row">
							<span>{t('language')}</span><strong>{localeLabels[locale]}</strong>
						</div>
						<div class="setting-row">
							<span>{t('theme')}</span><strong>{dark ? 'Dark' : 'Light'}</strong>
						</div>
					</article>
					<article class="panel">
						<div class="panel-head">
							<div>
								<p class="eyebrow">{t('engine')}</p>
								<h3>{t('upstreamEngine')}</h3>
							</div>
							<TerminalSquare size={19} />
						</div>
						<p class="body-copy">{t('inheritedEngine')}</p>
						<EngineManager
							status={engineStatus}
							updateOptions={engineUpdateOptions}
							policy={harborConfig?.engine_policy ?? 'auto'}
							{locale}
							busy={engineBusy}
							error={engineError}
							onPolicyChange={changeEnginePolicy}
							onUpdate={updateEngine}
							onRefresh={refreshEngineFromUi}
						/>
						<div class="setting-row">
							<span>{t('installationMode')}</span>
							<strong>
								{installationState?.helper_installed
									? t('installedAutomation')
									: t('portableForSetup')}
							</strong>
						</div>
					</article>
				</div>
				<article class="callout large">
					<ShieldCheck size={22} />
					<div>
						<strong>{t('safety')}</strong>
						<p>{t('safetyText')}</p>
					</div>
				</article>
			</section>
		{/if}

		<footer>
			<span><Anchor size={13} /> {t('footerNative')}</span>
		</footer>
	</main>
</div>
