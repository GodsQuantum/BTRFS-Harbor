import { describe, expect, it } from 'vitest';
import { dictionaries, type Locale } from './i18n';

describe('translations', () => {
	it('keeps exact key parity across every locale', () => {
		const locales = Object.keys(dictionaries) as Locale[];
		const reference = Object.keys(dictionaries.en).sort();

		for (const locale of locales) {
			expect(Object.keys(dictionaries[locale]).sort()).toEqual(reference);
		}
	});

	it('explains default Btrfs coverage in layman terms without claiming a full-disk image', () => {
		expect(dictionaries.fr.coverageSummary).toBe('Sauvegarde complète du système Btrfs');
		expect(dictionaries.fr.sourceSystem).toBe('Système (OS et logiciels)');
		expect(dictionaries.fr.coverageSummaryHelp).toContain(
			'OS, fichiers personnels et données persistantes'
		);
		expect(dictionaries.fr.coverageSummaryHelp.toLowerCase()).toContain(
			'pas une image complète du disque'
		);
	});

	it('labels the dedicated multi-job page as Scheduled Jobs', () => {
		expect(dictionaries.en.scheduledJobs).toBe('Scheduled Jobs');
		expect(dictionaries.fr.scheduledJobs).toBe('Tâches planifiées');
		expect(dictionaries['zh-CN'].scheduledJobs.length).toBeGreaterThan(0);
	});

	it('does not leave empty translated strings', () => {
		for (const dictionary of Object.values(dictionaries)) {
			expect(Object.values(dictionary).every((value) => value.trim().length > 0)).toBe(true);
		}
	});
});
