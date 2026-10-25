"""Focused local caption/split tests. No speech service or ASR downloads used."""
from pathlib import Path
import json
import shutil
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import silentdepth_captions as c


def heard(text,start=0.0,step=.4):
    return [{'word':token,'start':start+index*step,'end':start+(index+1)*step-.03,
             'probability':.99,'timestamp_method':'asr-word'} for index,token in enumerate(text.split())]


class AlignmentTests(unittest.TestCase):
    def test_exact_script_preserved_and_monotonic(self):
        text='The ocean floor records old events. A rock can preserve the evidence.'
        words=heard(text)
        tokens,times,check=c.align_words(text,words,0,6)
        self.assertEqual(check['flags'],[])
        self.assertEqual(check['matched_word_count'],len(text.split()))
        cues,notes=c.cues_for_beat(tokens,times,0,6)
        self.assertEqual(' '.join(' '.join(x['text'].split()) for x in cues),text)
        self.assertTrue(all(a['end']<=b['start'] for a,b in zip(cues,cues[1:])))
        self.assertTrue(all(len(line)<=44 for cue in cues for line in cue['text'].splitlines()))

    def test_unmatched_words_interpolate_between_real_anchors(self):
        text='The cold ocean water enters the rock.'
        words=heard(text)
        words[2]['word']='ancient'
        tokens,times,check=c.align_words(text,words,0,4)
        self.assertEqual(tokens[2],'ocean')
        self.assertEqual(times[2]['timing'],'unmatched-interpolation')
        self.assertGreaterEqual(times[2]['start'],times[1]['end'])
        self.assertLessEqual(times[2]['end'],times[3]['start'])
        self.assertIn('ocean',[run['text'] for run in check['unmatched_runs']])

    def test_missing_asr_is_flagged_not_certified(self):
        _,_,check=c.align_words('A long silent passage has no recognized speech.',[],0,5)
        self.assertIn('no_recognized_words',check['flags'])
        self.assertIn('insufficient_matching_anchors',check['flags'])

    def test_repeated_words_stay_ordered(self):
        text='The rock and the water and the rock react.'
        _,times,check=c.align_words(text,heard(text),0,6)
        self.assertEqual(check['flags'],[])
        self.assertTrue(all(a['end']<=b['start'] for a,b in zip(times,times[1:])))

    def test_segment_timestamp_fallback_is_disclosed(self):
        segment=SimpleNamespace(words=None,text='The ocean floor',start=1.0,end=4.0)
        records=c.segment_records(segment,offset=2)
        self.assertEqual(len(records),3)
        self.assertTrue(all(row['timestamp_method']=='segment-interpolation' for row in records))
        self.assertAlmostEqual(records[0]['start'],3)
        self.assertAlmostEqual(records[-1]['end'],6)

    def test_balanced_phrases_avoid_last_word_flash(self):
        tokens=('The ocean records events in rocks and sediments long after the water above has moved away').split()
        ranges=c.phrase_ranges(tokens)
        self.assertTrue(all(last-first>1 for first,last in ranges))
        self.assertTrue(all(c.format_two_lines(tokens[first:last]) is not None for first,last in ranges))

    def test_short_cue_can_fill_available_beat_time(self):
        tokens,times,_=c.align_words('Cold water.',heard('Cold water.',step=.15),0,2)
        cues,_=c.cues_for_beat(tokens,times,0,2)
        self.assertEqual(len(cues),1)
        self.assertGreaterEqual(cues[0]['end']-cues[0]['start'],1)

    def test_binding_rejects_stale_tampered_or_flagged_alignment(self):
        cues=[{'start':0.0,'end':2.0,'text':'Cold water.'}]
        report={'input_key':'current','cues':cues,'cues_sha256':c.p.digest(cues),'flagged_beats':[]}
        c.validate_report(report,'current','Cold water.',2)
        with self.assertRaises(c.p.PipelineError):
            c.validate_report(report,'changed','Cold water.',2)
        with self.assertRaises(c.p.PipelineError):
            c.validate_report({**report,'cues_sha256':'tampered'},'current','Cold water.',2)
        with self.assertRaises(c.p.PipelineError):
            c.validate_report({**report,'flagged_beats':[1]},'current','Cold water.',2)
        with self.assertRaises(c.p.PipelineError):
            c.validate_report(report,'current','A different script.',2)

    def test_overlap_rejected(self):
        cues=[{'start':0.0,'end':1.2,'text':'Cold'},{'start':1.1,'end':2.0,'text':'water.'}]
        report={'input_key':'current','cues':cues,'cues_sha256':c.p.digest(cues),'flagged_beats':[]}
        with self.assertRaises(c.p.PipelineError):
            c.validate_report(report,'current','Cold water.',2)

    def test_continuous_boundaries_preserve_adjacent_word_gap(self):
        beats=[{'id':1,'text':'The ocean is deep.'},{'id':2,'text':'The rocks preserve time.'}]
        words=heard(' '.join(beat['text'] for beat in beats))
        bounds,notes,whole,checks,flags=c.continuous_boundaries(beats,words,4)
        self.assertEqual(flags,[])
        self.assertTrue(all(not check['flags'] for check in checks))
        self.assertAlmostEqual(bounds[1],(words[3]['end']+words[4]['start'])/2)
        self.assertEqual(bounds[0],0)
        self.assertEqual(bounds[-1],4)


class SplitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='caption_test_',dir=ROOT/'tests')
        self.root=Path(self.temp.name)
        self.beats=[{'id':1,'text':'The ocean floor records old events.'},
                    {'id':2,'text':'A rock can preserve the evidence.'}]
        self.manifest={'beats':self.beats,'captions':{'alignment_file':'research/caption_alignment.json'}}
        c.p.save_json(self.root/'episode.json',self.manifest)
        self.source=self.root/'continuous.wav'
        with wave.open(str(self.source),'wb') as target:
            target.setparams((1,2,48000,0,'NONE','not compressed'))
            target.writeframes(b'\x01\x00\x02\x00'*(48000*2))
        self.identity={'path':'local-test-model','files_sha256':{'model.bin':'test-only'}}
        self.words=heard(' '.join(beat['text'] for beat in self.beats),start=.1,step=.25)
        self.fake_module=SimpleNamespace(WhisperModel=lambda *args,**kwargs:object())

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_sample_splits_and_caption_cache_reuse(self):
        with patch.object(c,'model_identity',return_value=self.identity),patch.object(c,'transcribe',return_value=(self.words,[])),patch.dict(sys.modules,{'faster_whisper':self.fake_module}):
            c.split_continuous(self.root,self.source,self.root/'model')
        report=c.p.read_json(self.root/'research/continuous_split.json')
        self.assertTrue(report['manifest_updated'])
        self.assertEqual(report['pcm_sha256'],report['concatenated_pcm_sha256'])
        imported=c.p.read_json(self.root/'episode.json')['beats']
        self.assertTrue(all(beat.get('narration_file') for beat in imported))
        frames=[]
        for beat in imported:
            with wave.open(str(self.root/beat['narration_file']),'rb') as wav:
                frames.append(wav.readframes(wav.getnframes()))
        with wave.open(str(self.source),'rb') as wav:
            self.assertEqual(b''.join(frames),wav.readframes(wav.getnframes()))
        (self.root/'audio').mkdir(exist_ok=True)
        shutil.copyfile(self.source,self.root/'audio/voiceover_v2.wav')
        cuts=report['cut_samples']
        timeline=[{'start_time':cuts[index]/48000,'end_time':cuts[index+1]/48000,'end_frame':round(cuts[index+1]*30/48000)} for index in range(len(self.beats))]
        with patch.object(c,'model_identity',return_value=self.identity),patch.object(c.p,'check_audio',return_value=timeline),patch.object(c.p,'caption_input_key',return_value='bound-test-key'),patch.object(c,'transcribe',side_effect=AssertionError('ASR should reuse identical PCM cache')):
            c.run(self.root,self.root/'model')
            c.run(self.root,None,inspect=True)
        alignment=c.p.read_json(self.root/'research/caption_alignment.json')
        self.assertEqual(alignment['flagged_beats'],[])
        self.assertEqual(alignment['input_key'],'bound-test-key')
        self.assertFalse(alignment['subjective_listening_performed'])
        self.assertTrue((self.root/'output/captions.srt').is_file())

    def test_split_retries_a_collapsed_word_timestamp(self):
        damaged=[dict(word) for word in self.words if word['word'].strip().lower()!='records']
        # The missing word has no vacant gap because the prior ASR word absorbs it.
        damaged[2]['end']=damaged[3]['start']
        replacement=[dict(word) for word in self.words[:6]]
        with patch.object(c,'model_identity',return_value=self.identity),patch.object(c,'transcribe',side_effect=[(damaged,[]),(replacement,[])]),patch.dict(sys.modules,{'faster_whisper':self.fake_module}):
            c.split_continuous(self.root,self.source,self.root/'model')
        report=c.p.read_json(self.root/'research/continuous_split.json')
        self.assertTrue(report['manifest_updated'])
        self.assertEqual(report['local_retried_beats'],[1])
        self.assertEqual(report['flagged_beats'],[])
        self.assertEqual(report['pcm_sha256'],report['concatenated_pcm_sha256'])

    def test_unresolved_split_saves_diagnostics_without_manifest_change(self):
        original=(self.root/'episode.json').read_bytes()
        with patch.object(c,'model_identity',return_value=self.identity),patch.object(c,'transcribe',return_value=([],[])),patch.dict(sys.modules,{'faster_whisper':self.fake_module}):
            with self.assertRaises(c.p.PipelineError):
                c.split_continuous(self.root,self.source,self.root/'model')
        report=c.p.read_json(self.root/'research/continuous_split.json')
        self.assertFalse(report['manifest_updated'])
        self.assertTrue(report['flagged_beats'])
        self.assertEqual(original,(self.root/'episode.json').read_bytes())
        self.assertFalse((self.root/'audio/imported').exists())

if __name__=='__main__':
    unittest.main()
