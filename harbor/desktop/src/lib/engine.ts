export type EnginePolicy = 'auto' | 'system' | 'bundled';
export type EngineOrigin = 'system' | 'bundled';

export interface EngineResolution {
	executable: string;
	origin: EngineOrigin;
	version: string | null;
}

export interface EngineCandidateStatus extends EngineResolution {
	compatible: boolean;
}

export interface EngineSelectionStatus {
	policy: EnginePolicy;
	active: EngineResolution;
	system: EngineCandidateStatus | null;
	bundled: EngineCandidateStatus | null;
	minimum_version: string;
	fallback_reason: string | null;
}

export type EngineUpdateStrategy =
	'harbor_application' | 'package_manager' | 'user_tool' | 'guidance_only';

export type EngineProvenance =
	'pacman' | 'dpkg' | 'rpm' | 'uv_tool' | 'pipx' | 'manual_unknown' | 'bundled_harbor';

export interface EngineUpdateOptions {
	strategy: EngineUpdateStrategy;
	can_update: boolean;
	package: string | null;
	provenance: EngineProvenance;
	reason: string;
}

export function engineHeadline(status: EngineSelectionStatus): string {
	const version = status.active.version ?? 'unknown';
	if (status.active.origin === 'system') {
		return `System ${version} · ${status.active.executable} · active`;
	}

	const staleSystem =
		status.system && !status.system.compatible
			? ` · system ${status.system.version ?? 'unknown'} incompatible`
			: '';
	return `Harbor bundled ${version} · active${staleSystem}`;
}

export function engineUpdatePresentation(options: EngineUpdateOptions): {
	label: string;
	actionable: boolean;
} {
	switch (options.strategy) {
		case 'harbor_application':
			return { label: 'Update Btrfs Harbor', actionable: options.can_update };
		case 'package_manager':
			return { label: 'Update system package', actionable: options.can_update };
		case 'user_tool':
			return { label: 'Update user tool', actionable: options.can_update };
		case 'guidance_only':
			return { label: 'Review update instructions', actionable: false };
	}
}
