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

	it('does not leave empty translated strings', () => {
		for (const dictionary of Object.values(dictionaries)) {
			expect(Object.values(dictionary).every((value) => value.trim().length > 0)).toBe(true);
		}
	});
});
