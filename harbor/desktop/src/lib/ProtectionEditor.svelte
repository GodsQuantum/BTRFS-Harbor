<script lang="ts">
	import { onMount } from 'svelte';
	import Check from 'lucide-svelte/icons/check';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import Save from 'lucide-svelte/icons/save';
	import Server from 'lucide-svelte/icons/server';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import {
		applyHarborConfiguration,
		chooseDestinationDirectory,
		discoverBtrfsSources,
		discoverMountedBackupDestinations,
		inspectDestinationMount,
		type MountProbe
	} from './agent';
	import {
		backupSourceFromDiscovery,
		cloneConfiguration,
		draftIssues,
		isSourceEnabled,
		recommendedSourcePaths,
		resolveDestination,
		resolveProfile,
		setSourceEnabled,
		type BackupProfile,
		type BackupSource,
		type DestinationKind,
		type DestinationSpec,
		type DiscoveredSource,
		type HarborConfig
	} from './config';
	import { translate, type Locale, type TranslationKey } from './i18n';
	import type { ProfileRuntime } from './status';

	export let config: HarborConfig;
	export let advanced = false;
	export let locale: Locale = 'en';
	export let runtime: ProfileRuntime | null | undefined = null;
	export let onApplied: () => Promise<void> | void = () => {};

	let applying = false;
	let message = '';
	let error = '';
	let discoveryError = '';
	let discoveredSources: DiscoveredSource[] = [];
	let detectedDestinations: MountProbe[] = [];

	$: profile = resolveProfile(config);
	$: destination = resolveDestination(config, profile);
	$: issues = draftIssues(config, profile);
	$: sourceChoices = buildSourceChoices(discoveredSources, profile, advanced);

	const t = (key: TranslationKey) => translate(locale, key);

	onMount(async () => {
		try {
			const [sources, destinations] = await Promise.all([
				discoverBtrfsSources(),
				discoverMountedBackupDestinations().catch(() => [])
			]);
			discoveredSources = sources;
			detectedDestinations = destinations;
			if (profile.sources.length === 0 && discoveredSources.length > 0) {
				const next = cloneConfiguration(config);
				const current = resolveProfile(next, profile.id);
				current.sources = discoveredSources
					.filter((source) => source.hint === 'recommended')
					.map(backupSourceFromDiscovery);
				config = next;
			}
		} catch (cause) {
			discoveryError = cause instanceof Error ? cause.message : String(cause);
		}
	});

	function clearFeedback() {
		message = '';
		error = '';
	}

	function fallbackSource(path: string): DiscoveredSource {
		const configured = profile.sources.find((source) => source.path === path);
		return {
			mount_point: path,
			source: configured?.path ?? path,
			subvolume: null,
			snapper_config: configured?.snapper_config ?? null,
			snapshot_count: 0,
			sendable_snapshot_count: 0,
			latest_snapshot_number: null,
			hint: recommendedSourcePaths.includes(path) ? 'recommended' : 'optional'
		};
	}

	function buildSourceChoices(
		discovered: DiscoveredSource[],
		current: BackupProfile,
		showAdvanced: boolean
	): DiscoveredSource[] {
		const byPath: Record<string, DiscoveredSource> = {};

		for (const source of discovered) byPath[source.mount_point] = source;
		for (const source of current.sources) {
			if (!(source.path in byPath)) byPath[source.path] = fallbackSource(source.path);
		}

		return Object.values(byPath)
			.filter(
				(source) =>
					showAdvanced ||
					source.hint === 'recommended' ||
					current.sources.some((configured) => configured.path === source.mount_point)
			)
			.sort((a, b) => {
				const rank = (source: DiscoveredSource) =>
					source.hint === 'recommended' ? 0 : source.hint === 'optional' ? 1 : 2;
				return rank(a) - rank(b) || a.mount_point.localeCompare(b.mount_point);
			});
	}

	function sourceHintLabel(source: DiscoveredSource): string {
		if (source.hint === 'recommended') return t('recommended');
		if (source.hint === 'usually_disposable') return t('usuallyDisposable');
		return t('optionalSource');
	}

	function timerLabel(): string {
		if (!runtime?.timer_installed) return t('timerNotInstalled');
		if (!runtime.timer_enabled || !runtime.timer_active) return t('timerDisabled');
		return t('timerActive');
	}

	function updateProfile(mutator: (profile: BackupProfile) => void) {
		const next = cloneConfiguration(config);
		const current = resolveProfile(next, profile.id);
		mutator(current);
		config = next;
		clearFeedback();
	}

	function updateDestination(mutator: (destination: DestinationSpec) => void) {
		const next = cloneConfiguration(config);
		const currentProfile = resolveProfile(next, profile.id);
		const current = resolveDestination(next, currentProfile);
		mutator(current);
		config = next;
		clearFeedback();
	}

	function updateSource(path: string, mutator: (source: BackupSource) => void) {
		const next = cloneConfiguration(config);
		const current = resolveProfile(next, profile.id);
		const source = current.sources.find((candidate) => candidate.path === path);
		if (!source) return;
		mutator(source);
		config = next;
		clearFeedback();
	}

	function toggleSource(source: DiscoveredSource, enabled: boolean) {
		const next = cloneConfiguration(config);
		const index = next.profiles.findIndex((candidate) => candidate.id === profile.id);
		const template = backupSourceFromDiscovery(source);
		next.profiles[index] = setSourceEnabled(
			next.profiles[index],
			source.mount_point,
			enabled,
			template
		);
		config = next;
		clearFeedback();
	}

	function useDetectedDestination(probe: MountProbe) {
		updateDestination((current) => {
			current.kind = probe.kind;
			current.path = probe.mount_point;
			current.mount_point = probe.mount_point;
			current.expected_mount_source = probe.source;
			if (current.name === 'Backup destination' || !current.name.trim()) {
				current.name = probe.kind === 'nfs' ? 'NFS backup' : 'SMB backup';
			}
		});
		message = t('autoDetectedMount');
	}

	async function browseDestination() {
		clearFeedback();
		const selected = await chooseDestinationDirectory(destination.path);
		if (!selected) return;

		let probe = null;
		let probeError = '';
		try {
			probe = await inspectDestinationMount(selected);
		} catch (cause) {
			probeError = cause instanceof Error ? cause.message : String(cause);
		}

		updateDestination((current) => {
			current.path = selected;
			if (!probe) return;
			current.kind = probe.kind;
			if (probe.kind === 'nfs' || probe.kind === 'smb') {
				current.mount_point = probe.mount_point;
				current.expected_mount_source = probe.source;
			} else {
				current.mount_point = null;
				current.expected_mount_source = null;
			}
		});

		if (probeError) error = probeError;
		else if (probe) message = t('autoDetectedMount');
	}

	function setDestinationKind(kind: DestinationKind) {
		updateDestination((current) => {
			current.kind = kind;
			if (kind !== 'nfs' && kind !== 'smb') {
				current.mount_point = null;
				current.expected_mount_source = null;
			}
		});
	}

	async function apply() {
		error = '';
		message = '';
		if (issues.length > 0) {
			error = t('completeRequiredFields') + ' ' + issues.join(', ');
			return;
		}

		applying = true;
		try {
			await applyHarborConfiguration(config, profile.id);
			message = t('configurationActivated');
			await onApplied();
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			applying = false;
		}
	}

	const schedules = [
		['hourly', 'hourlySchedule'],
		['*-*-* 02:00:00', 'dailySchedule'],
		['Sun *-*-* 02:00:00', 'weeklySchedule'],
		['*-*-01 02:00:00', 'scheduleMonthly']
	] as const;
</script>

[Reading 436 lines from start (total: 436 lines, 0 remaining)] [Reading 437 lines from start (total:
437 lines, 0 remaining)] [Reading 435 lines from start (total: 435 lines, 0 remaining)] [Reading 431
lines from start (total: 431 lines, 0 remaining)]

<div class="protection-editor">
	{#if !advanced}
		<div class="simple-defaults">
			<Check size={14} />
			<span>{t('simpleDefaults')}</span>
		</div>
	{/if}
	<div class="editor-grid" class:single={!advanced}>
		{#if advanced}
			<article class="panel editor-card">
				<div class="panel-head">
					<div>
						<p class="eyebrow">{t('profile')}</p>
						<h3>{profile.name || t('profileName')}</h3>
						<div class="runtime-summary">
							<span class:good-status={runtime?.timer_active} class:warn={!runtime?.timer_active}>
								{timerLabel()}
							</span>
							{#if runtime?.next_elapse_realtime}
								<small>{t('nextScheduledRun')}: {runtime.next_elapse_realtime}</small>
							{/if}
							{#if runtime?.last_trigger}
								<small>{t('lastTrigger')}: {runtime.last_trigger}</small>
							{/if}
						</div>
					</div>
					<span class="badge good"><ShieldCheck size={14} /> {t('recommended')}</span>
				</div>

				<label class="field">
					<span>{t('profileName')}</span>
					<input
						value={profile.name}
						oninput={(event) =>
							updateProfile(
								(current) => (current.name = (event.currentTarget as HTMLInputElement).value)
							)}
					/>
				</label>

				<label class="field">
					<span>{t('schedule')}</span>
					<select
						value={schedules.some(([value]) => value === profile.on_calendar)
							? profile.on_calendar
							: 'custom'}
						onchange={(event) => {
							const value = (event.currentTarget as HTMLSelectElement).value;
							if (value !== 'custom') updateProfile((current) => (current.on_calendar = value));
						}}
					>
						{#each schedules as [value, key] (value)}
							<option {value}>{t(key)}</option>
						{/each}
						<option value="custom">{t('advanced')}</option>
					</select>
				</label>

				{#if advanced}
					<label class="field">
						<span>{t('scheduleSpec')}</span>
						<input
							value={profile.on_calendar}
							oninput={(event) =>
								updateProfile(
									(current) =>
										(current.on_calendar = (event.currentTarget as HTMLInputElement).value)
								)}
						/>
					</label>

					<div class="retention-grid">
						{#each [['hourly', 'retentionHourly'], ['daily', 'dailyCopies'], ['weekly', 'weeklyCopies'], ['monthly', 'monthlyCopies'], ['yearly', 'retentionYearly']] as [field, key] (field)}
							<label class="field compact">
								<span>{t(key as TranslationKey)}</span>
								<input
									type="number"
									min="0"
									value={profile.retention[field as keyof typeof profile.retention]}
									oninput={(event) =>
										updateProfile((current) => {
											current.retention[field as keyof typeof current.retention] = Math.max(
												0,
												Number((event.currentTarget as HTMLInputElement).value) || 0
											);
										})}
								/>
							</label>
						{/each}
					</div>
				{/if}

				<label class="toggle-row">
					<input
						type="checkbox"
						checked={profile.verify_after_backup}
						onchange={(event) =>
							updateProfile(
								(current) =>
									(current.verify_after_backup = (event.currentTarget as HTMLInputElement).checked)
							)}
					/>
					<span>
						<strong>{t('verifyAfterBackup')}</strong>
						<small>SHA-256 · raw verify</small>
					</span>
				</label>
			</article>
		{/if}

		<article class="panel editor-card">
			<div class="panel-head">
				<div>
					<p class="eyebrow">{t('destination')}</p>
					<h3>{destination.name || t('destinationName')}</h3>
				</div>
				<Server size={20} />
			</div>

			<div class="field-row">
				<label class="field">
					<span>{t('destinationName')}</span>
					<input
						value={destination.name}
						oninput={(event) =>
							updateDestination(
								(current) => (current.name = (event.currentTarget as HTMLInputElement).value)
							)}
					/>
				</label>
				<label class="field">
					<span>{t('destinationType')}</span>
					<select
						value={destination.kind}
						onchange={(event) =>
							setDestinationKind(
								(event.currentTarget as HTMLSelectElement).value as DestinationKind
							)}
					>
						<option value="nfs">{t('nfs')}</option>
						<option value="smb">{t('smb')}</option>
						<option value="raw">{t('rawFolder')}</option>
						<option value="local">{t('localDisk')}</option>
						<option value="ssh">{t('ssh')}</option>
					</select>
				</label>
			</div>

			{#if detectedDestinations.length > 0 && !destination.path.trim()}
				<div class="detected-destinations">
					<strong>{t('detectedDestinations')}</strong>
					{#each detectedDestinations as probe (probe.mount_point + probe.source)}
						<button
							class="detected-destination"
							type="button"
							onclick={() => useDetectedDestination(probe)}
						>
							<span>
								<strong>{probe.mount_point}</strong>
								<small>{probe.kind.toUpperCase()} · {probe.source}</small>
							</span>
							<span class="badge good">{t('useDestination')}</span>
						</button>
					{/each}
				</div>
			{/if}

			<label class="field">
				<span>{t('targetPath')}</span>
				<div class="path-picker">
					<input
						value={destination.path}
						placeholder="/mnt/backup/machine"
						oninput={(event) =>
							updateDestination(
								(current) => (current.path = (event.currentTarget as HTMLInputElement).value)
							)}
					/>
					<button class="secondary" type="button" onclick={browseDestination}>
						<FolderOpen size={16} />
						{t('chooseFolder')}
					</button>
				</div>
			</label>

			{#if destination.kind === 'nfs' || destination.kind === 'smb'}
				<label class="field">
					<span>{t('mountPoint')}</span>
					<input
						value={destination.mount_point ?? ''}
						placeholder="/mnt/NAS"
						oninput={(event) =>
							updateDestination(
								(current) =>
									(current.mount_point = (event.currentTarget as HTMLInputElement).value || null)
							)}
					/>
				</label>
				<label class="field">
					<span>{t('expectedMountSource')}</span>
					<input
						value={destination.expected_mount_source ?? ''}
						placeholder={destination.kind === 'nfs' ? 'server:/share' : '//server/share'}
						oninput={(event) =>
							updateDestination(
								(current) =>
									(current.expected_mount_source =
										(event.currentTarget as HTMLInputElement).value || null)
							)}
					/>
				</label>
			{/if}

			{#if advanced}
				<label class="field">
					<span>{t('compression')}</span>
					<select
						value={destination.compression}
						onchange={(event) =>
							updateDestination(
								(current) =>
									(current.compression = (event.currentTarget as HTMLSelectElement).value)
							)}
					>
						<option value="zstd">zstd</option>
						<option value="none">none</option>
					</select>
				</label>
			{/if}
		</article>
	</div>

	<article class="panel editor-card">
		<div class="panel-head">
			<div>
				<p class="eyebrow">{t('selectVolumes')}</p>
				<h3>{t('sourceDetails')}</h3>
			</div>
			<span class="badge good">{profile.sources.length} enabled</span>
		</div>

		{#if discoveryError}
			<p class="error-text">{t('sourceDiscoveryFailed')}: {discoveryError}</p>
		{/if}
		<div class="source-editor-list">
			{#if sourceChoices.length === 0}
				<p class="empty-state">{t('noBtrfsSources')}</p>
			{/if}
			{#each sourceChoices as sourceChoice (sourceChoice.mount_point)}
				{@const path = sourceChoice.mount_point}
				<div class="source-editor-row">
					<label class="source-toggle">
						<input
							type="checkbox"
							checked={isSourceEnabled(profile, path)}
							onchange={(event) =>
								toggleSource(sourceChoice, (event.currentTarget as HTMLInputElement).checked)}
						/>
						<span class="check-box"><Check size={13} /></span>
						<code>{path}</code>
						<span class:good={sourceChoice.hint === 'recommended'} class="badge">
							{sourceHintLabel(sourceChoice)}
						</span>
						{#if sourceChoice.subvolume}
							<span class="badge">{sourceChoice.subvolume}</span>
						{/if}
						{#if sourceChoice.snapper_config}
							<span class="badge good">
								{t('snapperManaged')}: {sourceChoice.snapper_config}
							</span>
						{/if}
						{#if (sourceChoice.snapshot_count ?? 0) > 0}
							<span class="badge">
								{sourceChoice.snapshot_count}
								{t('snapshotCount').toLowerCase()}
							</span>
						{/if}
						{#if (sourceChoice.sendable_snapshot_count ?? 0) > 0}
							<span class="badge good">
								{sourceChoice.sendable_snapshot_count}
								{t('sendable')}
							</span>
						{/if}
					</label>

					{#if advanced && isSourceEnabled(profile, path)}
						{@const source = profile.sources.find((candidate) => candidate.path === path)}
						{#if source}
							<div class="source-advanced">
								<label class="field compact">
									<span>snapshot_prefix</span>
									<input
										value={source.snapshot_prefix}
										oninput={(event) =>
											updateSource(
												path,
												(current) =>
													(current.snapshot_prefix = (
														event.currentTarget as HTMLInputElement
													).value)
											)}
									/>
								</label>
								<label class="field compact">
									<span>snapper_config</span>
									<input
										value={source.snapper_config ?? ''}
										oninput={(event) =>
											updateSource(
												path,
												(current) =>
													(current.snapper_config =
														(event.currentTarget as HTMLInputElement).value || null)
											)}
									/>
								</label>
								<label class="field compact">
									<span>target_subdir</span>
									<input
										value={source.target_subdir}
										oninput={(event) =>
											updateSource(
												path,
												(current) =>
													(current.target_subdir = (event.currentTarget as HTMLInputElement).value)
											)}
									/>
								</label>
							</div>
						{/if}
					{/if}
				</div>
			{/each}
		</div>
	</article>

	<div class="editor-actions">
		<div class="editor-feedback">
			{#if error}<span class="error-text">{error}</span>{/if}
			{#if message}<span class="good-status">{message}</span>{/if}
		</div>
		<button class="primary" type="button" onclick={apply} disabled={applying || issues.length > 0}>
			<Save size={17} />
			{applying ? t('savingConfiguration') : t('saveActivate')}
		</button>
	</div>
</div>
