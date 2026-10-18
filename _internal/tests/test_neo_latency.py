import unittest
from unittest.mock import patch
from jarvis.runtime.latency import LatencyProfiler, trace


class LatencyTests(unittest.TestCase):
    def test_trace_restores_parent_and_does_not_leak_across_turns(self):
        profiler = LatencyProfiler()
        with trace('voice-1'):
            profiler.record('ASR', .1)
            with trace('telegram-1'):
                profiler.record('routing', .2)
            profiler.record('response', .3)
        profiler.record('background', .4)
        self.assertEqual([r['correlation_id'] for r in profiler.recent()], ['voice-1','telegram-1','voice-1',''])

    def test_percentiles_are_observed_values_and_ring_is_bounded(self):
        profiler = LatencyProfiler(max_events=10)
        for number in range(20):profiler.record('routing',number / 1000)
        stats=profiler.statistics()['routing']
        self.assertEqual(stats,{'samples':10,'p50_ms':14.0,'p95_ms':19.0,'p99_ms':19.0})
        profiler.record('routing',float('nan'))
        self.assertEqual(len(profiler.recent()),10)

    def test_marks_use_one_clock_and_no_trace_means_no_invented_sample(self):
        profiler = LatencyProfiler()
        profiler.mark('no trace')
        with patch('jarvis.runtime.latency.time.perf_counter',side_effect=[10,10.2,10.5]):
            with trace('command'):
                profiler.mark('transcript_to_intent')
                profiler.mark('intent_to_action')
        rows=profiler.recent()
        self.assertEqual(len(rows),2)
        self.assertAlmostEqual(rows[0]['seconds'],.2)
        self.assertAlmostEqual(rows[1]['seconds'],.3)
