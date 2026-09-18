"""Headless NiceGUI container tests (run with the gui Python environment)."""
import copy
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'src')]
try:
    from nicegui import ui
except ModuleNotFoundError:
    raise unittest.SkipTest('UI 测试请使用 gui Python 环境')
import demo_ui as demo
from wind_ui import WindPanel


class WindLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        demo._ui_state.clear()
        cls.wind = WindPanel(ROOT, lambda: (demo._DATA_DIR(), demo._SUMMARY(), None), [], {})
        cls.callbacks = []
        cls.summary = demo.load_summary()
        with ui.column() as cls.container:
            demo.build_left_panel(cls.summary, cls.callbacks, cls.wind)
            demo._build_right_panel_with_refresh(cls.summary, cls.callbacks, cls.wind)
        cls.elements = list(cls.container.descendants())

    def find(self, kind, label):
        return next(e for e in self.elements if isinstance(e, kind)
                    and (getattr(e, 'text', None) == label or e._props.get('label') == label))

    def test_left_switch_preserves_hidden_inputs_and_parameters(self):
        before = copy.deepcopy(demo._collect_overrides())
        ids = {key: control.id for key, control in demo._inputs.items()}
        selector = self.find(ui.select, '其他输入与管理')
        history = self.find(ui.expansion, '历史记录')
        instrument = self.find(ui.expansion, '仪器参数')
        self.assertFalse(history.visible)
        selector.value = '历史记录'
        self.assertTrue(history.visible)
        self.assertTrue(instrument.visible)
        selector.value = '雾 — 场景参数'
        self.assertFalse(history.visible)
        self.assertTrue(self.find(ui.expansion, '雾 — 场景参数').visible)
        selector.value = '收起'
        self.assertEqual(before, demo._collect_overrides())
        self.assertEqual(ids, {key: control.id for key, control in demo._inputs.items()})
        for label in ('写入并重算', '恢复默认', '查看日志', '计算测风'):
            button = self.find(ui.button, label)
            self.assertTrue(button.visible)
            parent = button.parent_slot.parent
            while parent != self.container:
                self.assertTrue(parent.visible)
                parent = parent.parent_slot.parent

    def test_two_primary_entries_and_independent_checkboxes(self):
        primary = next(e for e in self.elements if isinstance(e, ui.toggle)
                       and e.options == ['风速', '其他结果'])
        self.assertEqual(primary.value, '风速')
        fog = self.find(ui.checkbox, '雾 · 功率')
        rain = self.find(ui.checkbox, '雨 · 功率')
        with patch.object(ui, 'timer'):
            primary.value = '其他结果'
            fog.value, rain.value = True, True
            self.assertEqual(demo._ui_state['other_results'], ['雾 · 功率', '雨 · 功率'])
            panels = [e for e in self.elements if isinstance(e, ui.column)
                      and any(isinstance(c, ui.label) and c.text in ('雾 · 功率', '雨 · 功率')
                              for c in e.default_slot.children)]
            self.assertEqual(len(panels), 2)
            self.assertTrue(all(p.visible for p in panels))
            primary.value = '风速'
            self.assertTrue(fog.value and rain.value)
            self.assertEqual(demo._ui_state['active_tab'], '风速')

    def test_empty_wind_page_and_independent_parameter_registry(self):
        self.assertIn('尚无测风结果', self.wind.summary.text)
        self.assertEqual(self.wind.spectrum.options['series'], [])
        self.assertEqual(self.wind.histogram.options['series'], [])
        self.assertFalse(set(self.wind.inputs.values()) & set(demo._inputs.values()))
        self.assertGreaterEqual(len(self.callbacks), 13)


class WindSubprocessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.host = ui.column()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'src').mkdir()
        for name in ('wind_io.py', 'wind_worker.py', 'wind_receiver.py', 'spad_detector.py'):
            shutil.copy2(ROOT / 'src' / name, self.root / 'src' / name)
        self.summary_path = self.root / 'summary.json'
        self.summary_path.write_text(json.dumps({'global': {'wavelength_nm': 1550,
            'instrument_parameters': {'pulse_width_s': 2e-7}}}), encoding='utf-8')
        (self.root / 'maritime_haze_power.csv').write_text('range_m,power_signal_raw\n1000,2e-11\n', encoding='utf-8')
        self.log = []
        with self.host:
            self.wind = WindPanel(self.root, lambda: (self.root, self.summary_path, 'fixture'), self.log, {})
            self.wind.build_inputs()
            self.wind.build_controls()
            self.wind.build_results()
        self.python_patch = patch('wind_ui.resolve_mie_python_executable',
                                  return_value=str(ROOT / '.pixi/envs/mie/python.exe'))
        self.python_patch.start()

    async def asyncTearDown(self):
        self.python_patch.stop()
        self.temp.cleanup()

    async def test_run_render_export_data_and_stale_detection(self):
        self.wind.inputs['pulses'].value = 300
        await self.wind.run()
        self.assertIsNotNone(self.wind.result, self.log)
        self.assertEqual(self.wind.result['source']['run_id'], 'fixture')
        self.assertEqual(len(self.wind.spectrum.options['series']), 2)
        self.assertEqual(len(self.wind.histogram.options['series']), 1)
        self.assertNotIn('已过期', self.wind.summary.text)
        self.wind.inputs['velocity_m_s'].value = -1
        self.assertIn('已过期', self.wind.summary.text)
        self.wind.inputs['velocity_m_s'].value = 5
        (self.root / 'maritime_haze_power.csv').write_text('range_m,power_signal_raw\n1000,3e-11\n', encoding='utf-8')
        self.wind.refresh()
        self.assertIn('已过期', self.wind.summary.text)
        self.assertFalse(self.wind.running)
        self.assertIsNone(self.wind.process)

    async def test_cancel_releases_worker(self):
        self.wind.inputs['pulses'].value = 20000
        self.wind.inputs['max_lag_ns'].value = 100
        task = asyncio.create_task(self.wind.run())
        async with asyncio.timeout(10):
            while self.wind.process is None and not task.done():
                await asyncio.sleep(0.01)
            self.assertIsNotNone(self.wind.process, self.log)
            self.wind.cancel()
            await task
        self.assertIn('已取消', self.wind.status.text)
        self.assertIsNone(self.wind.result)
        self.assertIsNone(self.wind.process)
        self.assertFalse(self.wind.running)


if __name__ == '__main__':
    unittest.main()
