<script lang="ts">
	import { onMount } from 'svelte';
	import Check from 'lucide-svelte/icons/check';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import Save from 'lucide-svelte/icons/save';
	import Server from 'lucide-svelte/icons/server';
	import LifeBuoy from 'lucide-svelte/icons/life-buoy';
	import {
		applyHarborConfiguration,
		savePortableHarborConfiguration,
		chooseDestinationDirectory,
		discoverBtrfsSources,
		inspectDestinationMount,
		loadInstallationState,
		type InstallationState
	} from './agent';
	import {
		backupSourceFromDiscovery,
		cloneConfiguration,
		describeDraftIssue,
		draftIssues,
		formatSshDestination,
		isSourceEnabled,
		parseSshDestination,
		resolveDestination,
		resolveProfile,
		setSourceEnabled,
		sourceDisplayName,
		type BackupProfile,
		type BackupSource,
		type DestinationSpec,
		type DiscoveredSource,
		type HarborConfig,
		type SshDestinationFields
	} from './config';
	import CheckpointControls from './CheckpointControls.svelte';
	import { translate, type Locale, type TranslationKey } from './i18n';
	import { type ProfileRuntime } from './status';

	export let config: HarborConfig;
	export let profileId: string | undefined = undefined;
	export let locale: Locale = 'en';
	export let runtime: ProfileRuntime | null | undefined = null;
	export let onApplied: () => Promise<void> | void = () => {};
	export let onRecover: () => Promise<void> | void = () => {};

	let applying = false;
	let installation: InstallationState | null = null;
	let message = '';
	let error = '';
	let discoveryError = '';
	let destinationValidated = false;
	let validatingDestination = false;
	let discoveredSources: DiscoveredSource[] = [];
	let sshFields: SshDestinationFields = { user: '', host: '', port: 22, path: '/backups' };

	$: profile = resolveProfile(config, profileId);
	$: destination = resolveDestination(config, profile);
	$: sourceChoices = buildSourceChoices(discoveredSources, profile);
	$: destinationIsSsh = destination.kind === 'ssh';
	$: hasSnapperSource = profile.sources.some((source) => Boolean(source.snapper_config));

	const t = (key: TranslationKey) => translate(locale, key);

	onMount(async () => {
		if (destination.kind === 'ssh') sshFields = parseSshDestination(destination.path);
		try {
			installation = await loadInstallationState();
		} catch {
			installation = null;
		}
		try {
			discoveredSources = await discoverBtrfsSources();
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

	function focusSection(section: 'profile' | 'schedule' | 'sources' | 'destination') {
		requestAnimationFrame(() => {
			document
				.getElementById('harbor-' + section)
				?.scrollIntoView({ behavior: 'smooth', block: 'center' });
		});
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
			hint: 'optional'
		};
	}

	function buildSourceChoices(
		discovered: DiscoveredSource[],
		current: BackupProfile
	): DiscoveredSource[] {
		const byPath: Record<string, DiscoveredSource> = {};
		for (const source of discovered) byPath[source.mount_point] = source;
		for (const source of current.sources) {
			if (!(source.path in byPath)) byPath[source.path] = fallbackSource(source.path);
		}
		return Object.values(byPath).sort((a, b) => {
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

	function sourceName(path: string): string {
		if (path === '/') return t('sourceSystem');
		if (path === '/home') return t('sourcePersonalFiles');
		if (path === '/srv') return t('sourceData');
		if (path === '/root') return t('sourceAdministratorFiles');
		return sourceDisplayName(path);
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
		destinationValidated = false;
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
		next.profiles[index] = setSourceEnabled(
			next.profiles[index],
			source.mount_point,
			enabled,
			backupSourceFromDiscovery(source)
		);
		config = next;
		clearFeedback();
	}

	async function browseDestination() {
		clearFeedback();
		const selected = await chooseDestinationDirectory(destination.path);
		if (!selected) return;
		updateDestination((current) => {
			current.kind = 'raw';
			current.path = selected;
			current.mount_point = null;
			current.expected_mount_source = null;
		});
		await validateDestination();
	}

	function setDestinationMode(mode: 'folder' | 'ssh') {
		const wasSsh = destination.kind === 'ssh';
		updateDestination((current) => {
			current.kind = mode === 'ssh' ? 'ssh' : 'raw';
			current.mount_point = null;
			current.expected_mount_source = null;
			if ((mode === 'ssh') !== wasSsh) current.path = '';
		});
		if (mode === 'ssh') {
			sshFields = wasSsh
				? parseSshDestination(destination.path)
				: { user: '', host: '', port: 22, path: '/backups' };
		}
	}

	function setSshFields(next: SshDestinationFields) {
		sshFields = next;
		updateDestination((current) => {
			current.kind = 'ssh';
			current.path = formatSshDestination(next);
			current.mount_point = null;
			current.expected_mount_source = null;
		});
	}

	async function normalizedConfiguration(): Promise<HarborConfig> {
		const next = cloneConfiguration(config);
		const currentProfile = resolveProfile(next, profile.id);
		const current = resolveDestination(next, currentProfile);

		if (current.kind === 'ssh') {
			if (!sshFields.host.trim() || !sshFields.path.trim() || sshFields.path.trim() === '/') {
				throw new Error(t('invalidSshDestination'));
			}
			current.path = formatSshDestination(sshFields);
			current.mount_point = null;
			current.expected_mount_source = null;
			return next;
		}

		if (!current.path.trim()) throw new Error(t('destinationNeedsPath'));
		const probe = await inspectDestinationMount(current.path);
		if (!probe) {
			current.kind = 'raw';
			current.mount_point = null;
			current.expected_mount_source = null;
			return next;
		}
		current.kind = probe.kind;
		if (probe.kind === 'nfs' || probe.kind === 'smb') {
			current.mount_point = probe.mount_point;
			current.expected_mount_source = probe.source;
		} else {
			current.kind = 'raw';
			current.mount_point = null;
			current.expected_mount_source = null;
		}
		return next;
	}

	async function validateDestination() {
		error = '';
		message = '';
		validatingDestination = true;
		try {
			const next = await normalizedConfiguration();
			config = next;
			destinationValidated = true;
			message = t('destinationReady');
		} catch (cause) {
			destinationValidated = false;
			error = cause instanceof Error ? cause.message : String(cause);
			focusSection('destination');
		} finally {
			validatingDestination = false;
		}
	}

	async function validatedDraft(): Promise<{
		config: HarborConfig;
		profile: BackupProfile;
	} | null> {
		const next = await normalizedConfiguration();
		const nextProfile = resolveProfile(next, profile.id);
		const nextIssues = draftIssues(next, nextProfile);
		if (nextIssues.length > 0) {
			const issue = describeDraftIssue(nextIssues[0]);
			error = t(issue.messageKey);
			focusSection(issue.section);
			return null;
		}
		config = next;
		return { config: next, profile: nextProfile };
	}

	async function apply() {
		error = '';
		message = '';
		applying = true;
		try {
			const draft = await validatedDraft();
			if (!draft) return;
			if (!installation?.helper_installed) {
				await savePortableHarborConfiguration(draft.config);
				message =
					locale === 'fr'
						? 'Configuration portable enregistrée. Aucun service ni minuteur installé.'
						: locale === 'zh-CN'
							? '便携备份配置已保存，未安装服务或定时任务。'
							: 'Portable backup configuration saved. No service or timer installed.';
			} else {
				await applyHarborConfiguration(draft.config, draft.profile.id);
				message = t('configurationActivated');
			}
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
		['@snapshots', 'snapshotSchedule']
	] as const;
</script>

<div class="protection-editor">
	<article class="panel editor-card source-section" id="harbor-sources">
		<div class="panel-head">
			<div>
				<p class="eyebrow">{t('whatBackedUp')}</p>
				<h3>{t('coverageSummary')}</h3>
				<small class="field-help">{t('coverageSummaryHelp')}</small>
			</div>
			<span class="badge good">{profile.sources.length} {t('sourcesSelected')}</span>
		</div>

		{#if discoveryError}
			<p class="error-text">{t('sourceDiscoveryFailed')}: {discoveryError}</p>
		{/if}
		<div class="source-editor-list compact-sources">
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
						<span class="source-label">
							<strong>{sourceName(path)}</strong>
							<small
								><code>{path}</code>{sourceChoice.subvolume
									? ' · ' + sourceChoice.subvolume
									: ''}</small
							>
						</span>
						<span class:good={sourceChoice.hint === 'recommended'} class="badge">
							{sourceHintLabel(sourceChoice)}
						</span>
						{#if sourceChoice.snapper_config}
							<span class="badge good">{t('snapperManaged')}</span>
						{/if}
						{#if (sourceChoice.snapshot_count ?? 0) > 0}
							<span class="badge">
								{sourceChoice.snapshot_count}
								{t('snapshotCount').toLowerCase()}
							</span>
						{/if}
					</label>
					{#if isSourceEnabled(profile, path)}
						{@const source = profile.sources.find((candidate) => candidate.path === path)}
						<details class="advanced-details source-options">
							<summary>{t('technicalDetails')} <ChevronDown size={13} /></summary>
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
														(current.target_subdir = (
															event.currentTarget as HTMLInputElement
														).value)
												)}
										/>
									</label>
								</div>
							{/if}
						</details>
					{/if}
				</div>
			{/each}
		</div>
	</article>

	<article class="panel editor-card destination-section" id="harbor-destination">
		<div class="panel-head">
			<div>
				<p class="eyebrow">{t('destination')}</p>
				<h3>{t('backupDestination')}</h3>
			</div>
			<Server size={20} />
		</div>

		<div class="destination-mode" role="group" aria-label={t('destinationType')}>
			<button
				type="button"
				class:active={!destinationIsSsh}
				onclick={() => setDestinationMode('folder')}
			>
				<FolderOpen size={15} />
				{t('folderDestination')}
			</button>
			<button
				type="button"
				class:active={destinationIsSsh}
				onclick={() => setDestinationMode('ssh')}
			>
				<Server size={15} />
				{t('sshDestination')}
			</button>
		</div>

		{#if destinationIsSsh}
			<div class="ssh-grid">
				<label class="field">
					<span>{t('sshHost')}</span>
					<input
						value={sshFields.host}
						placeholder="192.0.2.10"
						oninput={(event) =>
							setSshFields({ ...sshFields, host: (event.currentTarget as HTMLInputElement).value })}
					/>
				</label>
				<label class="field">
					<span>{t('sshUser')}</span>
					<input
						value={sshFields.user}
						placeholder="backup"
						oninput={(event) =>
							setSshFields({ ...sshFields, user: (event.currentTarget as HTMLInputElement).value })}
					/>
				</label>
				<label class="field">
					<span>{t('sshPort')}</span>
					<input
						type="number"
						min="1"
						max="65535"
						value={sshFields.port}
						oninput={(event) =>
							setSshFields({
								...sshFields,
								port: Number((event.currentTarget as HTMLInputElement).value) || 22
							})}
					/>
				</label>
				<label class="field ssh-folder">
					<span>{t('sshFolder')}</span>
					<input
						value={sshFields.path}
						placeholder="/backups/workstation"
						oninput={(event) =>
							setSshFields({ ...sshFields, path: (event.currentTarget as HTMLInputElement).value })}
					/>
				</label>
			</div>
			<div class="destination-actions">
				<button
					class="secondary validation-button"
					class:validated={destinationValidated}
					type="button"
					disabled={validatingDestination}
					onclick={validateDestination}
				>
					<Check size={15} />
					{destinationValidated ? t('destinationReady') : t('testConnection')}
				</button>
			</div>
		{:else}
			<label class="field">
				<span>{t('backupFolder')}</span>
				<div class="path-picker">
					<input
						value={destination.path}
						placeholder="/mnt/backup"
						oninput={(event) =>
							updateDestination(
								(current) => (current.path = (event.currentTarget as HTMLInputElement).value)
							)}
					/>
					<button class="secondary" type="button" onclick={browseDestination}>
						<FolderOpen size={16} />
						{t('chooseFolder')}
					</button>
					<button
						class="secondary validation-button"
						class:validated={destinationValidated}
						type="button"
						disabled={validatingDestination}
						onclick={validateDestination}
					>
						<Check size={15} />
						{destinationValidated ? t('destinationReady') : t('validateDestination')}
					</button>
				</div>
				<small class="field-help">{t('folderDestinationHelp')}</small>
			</label>
		{/if}

		<details class="advanced-details">
			<summary>{t('moreOptions')} <ChevronDown size={13} /></summary>
			<div class="details-grid">
				<label class="field compact">
					<span>{t('destinationName')}</span>
					<input
						value={destination.name}
						oninput={(event) =>
							updateDestination(
								(current) => (current.name = (event.currentTarget as HTMLInputElement).value)
							)}
					/>
				</label>
				<label class="field compact">
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
			</div>
		</details>
	</article>

	<section class="simple-schedule schedule-section" id="harbor-schedule">
		<div>
			<strong>{t('schedule')}</strong>
			<small>{t('simpleScheduleHelp')}</small>
		</div>
		<div class="schedule-options">
			{#each schedules as [value, key] (value)}
				<button
					type="button"
					class:active={profile.on_calendar === value}
					disabled={value === '@snapshots' && !hasSnapperSource}
					title={value === '@snapshots' && !hasSnapperSource
						? t('snapshotScheduleNeedsSnapper')
						: ''}
					onclick={() => updateProfile((current) => (current.on_calendar = value))}
				>
					{t(key)}
				</button>
			{/each}
		</div>
		{#if runtime}
			<small class:good-status={runtime.timer_active}>{timerLabel()}</small>
		{:else if !installation?.helper_installed}
			<small class="field-help">{t('scheduleInactiveUntilInstalled')}</small>
		{/if}
		<details class="advanced-details">
			<summary>{t('moreOptions')} <ChevronDown size={13} /></summary>
			<div class="details-grid">
				<label class="field compact" id="harbor-profile">
					<span>{t('profileName')}</span>
					<input
						value={profile.name}
						oninput={(event) =>
							updateProfile(
								(current) => (current.name = (event.currentTarget as HTMLInputElement).value)
							)}
					/>
				</label>
				<label class="field compact">
					<span>{t('scheduleSpec')}</span>
					<input
						disabled={profile.on_calendar === '@snapshots'}
						value={profile.on_calendar}
						oninput={(event) =>
							updateProfile(
								(current) => (current.on_calendar = (event.currentTarget as HTMLInputElement).value)
							)}
					/>
				</label>
			</div>
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
		</details>
	</section>

	<CheckpointControls {profile} {destination} {locale} />

	<div class="editor-actions">
		<div class="editor-feedback">
			{#if error}<span class="error-text">{error}</span>{/if}
			{#if message}<span class="good-status">{message}</span>{/if}
			{#if !error && !message && !installation?.helper_installed}
				<span class="field-help">{t('portableManualHelp')}</span>
			{/if}
		</div>
		<div class="editor-action-buttons">
			<button class="secondary" type="button" onclick={onRecover} disabled={applying}>
				<LifeBuoy size={16} />
				{t('recoverFromSnapshot')}
			</button>
			<button class="primary" type="button" onclick={apply} disabled={applying}>
				<Save size={17} />
				{applying
					? t('savingConfiguration')
					: installation?.helper_installed
						? t('saveActivate')
						: locale === 'fr'
							? 'Enregistrer'
							: locale === 'zh-CN'
								? '保存'
								: 'Save portable profile'}
			</button>
		</div>
	</div>
</div>
