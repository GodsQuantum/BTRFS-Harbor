import { describe, expect, it } from 'vitest';
import {
	aggregateTransferProgress,
	formatBytesBinary,
	formatDuration,
	formatRateBinary,
	upsertTransferProgress,
	type TransferProgressEvent
} from './status';

function transfer(overrides: Partial<TransferProgressEvent> = {}): TransferProgressEvent {
	return {
		schema: 1,
		event: 'transfer_progress',
		volume: '/home',
		snapshot: 'home-20261008',
		destination: '/backup/home',
		bytes_target: 1024,
		bytes_source: null,
		total_estimate: null,
		estimate_kind: null,
		bytes_per_second: 512,
		elapsed_seconds: 2,
		eta_seconds: null,
		measurement: 'raw-target-file',
		certainty: 'unknown-total',
		...overrides
	};
}

describe('transfer progress aggregation', () => {
	it('keeps one latest event per volume/snapshot/destination', () => {
		const first = transfer({ bytes_target: 1024 });
		const latest = transfer({ bytes_target: 2048 });
		const events = upsertTransferProgress(upsertTransferProgress([], first), latest);
		expect(events).toHaveLength(1);
		expect(events[0].bytes_target).toBe(2048);
	});

	it('sums real bytes and rates across parallel transfers', () => {
		const summary = aggregateTransferProgress([
			transfer({ volume: '/', bytes_target: 1024, bytes_per_second: 256, elapsed_seconds: 8 }),
			transfer({
				volume: '/home',
				snapshot: 'home2',
				bytes_target: 3072,
				bytes_per_second: 768,
				elapsed_seconds: 10
			})
		]);
		expect(summary.bytesTarget).toBe(4096);
		expect(summary.bytesPerSecond).toBe(1024);
		expect(summary.elapsedSeconds).toBe(10);
	});

	it('never invents percentage or eta when final target size is unknown', () => {
		const summary = aggregateTransferProgress([transfer()]);
		expect(summary.percent).toBeNull();
		expect(summary.etaSeconds).toBeNull();
		expect(summary.estimated).toBe(false);
	});

	it('uses estimated percentage only when every active transfer has comparable totals', () => {
		const summary = aggregateTransferProgress([
			transfer({
				bytes_target: 25,
				total_estimate: 100,
				certainty: 'estimated-total',
				bytes_per_second: 5
			}),
			transfer({
				volume: '/srv',
				snapshot: 'srv',
				bytes_target: 25,
				total_estimate: 100,
				certainty: 'estimated-total',
				bytes_per_second: 5
			})
		]);
		expect(summary.percent).toBe(25);
		expect(summary.etaSeconds).toBe(15);
		expect(summary.estimated).toBe(true);
	});
});

describe('transfer progress formatting', () => {
	it('formats bytes, rates and duration without decimal-SI ambiguity', () => {
		expect(formatBytesBinary(1073741824)).toBe('1.00 GiB');
		expect(formatRateBinary(1048576)).toBe('1.00 MiB/s');
		expect(formatDuration(3661)).toBe('1:01:01');
	});
});
