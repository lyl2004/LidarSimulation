"""Wind UI: stdlib plus NiceGUI; numerical work stays in a hidden subprocess."""
import asyncio
import json
import os
import subprocess
import uuid

from nicegui import ui

from path_resolver import resolve_mie_python_executable
from wind_io import DEFAULTS, SCENES, make_request, read_source


class WindPanel:
    def __init__(self, root, source_paths, log, state):
        self.root, self.source_paths, self.log, self.state = root, source_paths, log, state
        self.inputs = {}
        self.process = None
        self.running = False
        self.cancelled = False
        self.result = state.get('result')
        self.status = None
        self.client = ui.context.client
        self.client.on_delete(self.cancel)

    def build_inputs(self):
        with ui.expansion('测风接收机参数', icon='air', value=True).classes('w-full'):
            ui.label('修改仪器参数后先“写入并重算”，再点击“计算测风”。').classes('text-xs text-amber-600 italic mb-1')
            self.scene = ui.select(SCENES, value=self.state.get('scene', 'maritime_haze'), label='测风来源场景').props('dense outlined options-dense').classes('w-full text-sm')
            self.distance = self._number('测量距离 (m)', self.state.get('distance', 1000), min=0)
            labels = {
                'velocity_m_s': '设定径向速度 (m/s)',
                'lo_power_w': '本振功率 (W)', 'reference_mhz': '参考频差 (MHz)',
                'pulses': '累计脉冲数', 'window_ns': '有效接收窗口 (ns，不得超过脉宽)',
                'dt_ns': '探测时间步长 (ns)', 'prf_hz': '脉冲重复频率 (Hz)',
                'efficiency': 'SPAD 探测效率 (0～1)', 'dead_ns': '死时间 (ns)',
                'dark_hz': '每通道暗计数率 (Hz)', 'background_power_w': '混频前总背景光功率 (W)',
                'visibility': '干涉可见度 (0～1)', 'split': '通道 1 分光比例',
                'afterpulse_probability': '后脉冲概率 (每次原生雪崩)',
                'afterpulse_tau_ns': '后脉冲平均释放延迟 (ns)', 'max_lag_ns': '时间差上限 (ns)',
                'search_min_mhz': '寻峰下限 (MHz)', 'search_max_mhz': '寻峰上限 (MHz)',
                'min_snr_db': '有效测风最低谱 SNR (dB)', 'seed': '随机种子',
            }
            values = {**DEFAULTS, **self.state.get('settings', {})}
            for key in ('velocity_m_s', 'lo_power_w', 'pulses'):
                self.inputs[key] = self._number(labels[key], values[key])
            with ui.expansion('采样、探测器与寻峰设置', icon='tune').classes('w-full'):
                for key, label in labels.items():
                    if key not in self.inputs:
                        self.inputs[key] = self._number(label, values[key])
            for control in [self.scene, self.distance, *self.inputs.values()]:
                control.on_value_change(self.changed)

    @staticmethod
    def _number(label, value, **kwargs):
        with ui.row().classes('items-center gap-1 w-full flex-nowrap'):
            ui.label(label).classes('text-sm text-gray-500 shrink-0').style('width:112px; overflow-wrap:anywhere')
            return ui.number(value=value, format='%.10g', **kwargs).props(
                'dense outlined hide-bottom-space').classes('text-sm font-mono flex-1 min-w-0').tooltip(label)

    def build_controls(self):
        with ui.row().classes('gap-2 flex-wrap'):
            self.run_button = ui.button('计算测风', on_click=self.run, icon='air').props('dense')
            self.cancel_button = ui.button('取消测风', on_click=self.cancel).props('dense flat')
            self.cancel_button.disable()
            ui.button('恢复测风默认', on_click=self.reset).props('dense flat')
        self.status = ui.label('测风尚未运行').classes('text-xs text-gray-600')

    def settings(self):
        return {key: control.value for key, control in self.inputs.items()}

    def request(self):
        data, summary, run_id = self.source_paths()
        source = read_source(data, summary, run_id, self.scene.value, self.distance.value)
        return make_request(source, self.settings())

    def changed(self, _event=None):
        self.state.update(scene=self.scene.value, distance=self.distance.value, settings=self.settings())
        if hasattr(self, 'summary'):
            self.refresh()

    def reset(self):
        for key, value in DEFAULTS.items():
            self.inputs[key].value = value
        self.scene.value = 'maritime_haze'
        self.distance.value = 1000
        self.changed()

    def build_results(self):
        with ui.column().classes('w-full bg-white rounded-lg shadow-sm mt-2 p-5 gap-4'):
            ui.label('风速 · 双通道光子探测').classes('text-lg font-semibold')
            self.summary = ui.label('尚无测风结果：选定来源、距离与参数后，点击左侧“计算测风”。').classes('text-sm text-gray-600')
            with ui.row().classes('w-full items-center gap-3 flex-wrap'):
                ui.label('滚轮缩放 / 拖动底部滑块查看区间 / 单击图例切换曲线').classes('text-xs text-gray-400 italic')
            self.spectrum = ui.echart(self._chart_options(
                '相关函数 FFT 频谱', '频率 (MHz)', '单边谱功率', legend=True)).classes('w-full').style('height:420px')
            self.histogram = ui.echart(self._chart_options(
                '双通道事件时间差直方图', 't₁ − t₂ (ns)', '事件对数')).classes('w-full').style('height:420px')
            with ui.expansion('数值摘要', icon='table_chart', value=True).classes('w-full'):
                self.detail = ui.label('计算完成后显示结果摘要。').classes('text-xs text-gray-600 whitespace-pre-wrap')
            with ui.row().classes('gap-2 items-center'):
                ui.label('下载：').classes('text-sm text-gray-400')
                self.export = ui.button('导出测风结果 JSON', icon='download', on_click=self.download).props('dense flat').classes('text-sm font-mono text-green-700')
        self.refresh()

    @staticmethod
    def _chart_options(title, x_label, y_label, *, legend=False):
        options = {
            'title': {'text': title, 'left': 'center', 'textStyle': {'fontSize': 16, 'fontWeight': 'normal'}},
            'textStyle': {'fontFamily': 'Arial, sans-serif', 'fontSize': 12, 'color': '#374151'},
            'color': ['#2563eb', '#6b7280'],
            'grid': {'left': 65, 'right': 35, 'top': 85, 'bottom': 85, 'containLabel': True},
            'xAxis': {'type': 'value', 'name': x_label, 'nameLocation': 'middle', 'nameGap': 30},
            'yAxis': {'type': 'value', 'name': y_label, 'splitLine': {'lineStyle': {'color': '#e5e7eb'}}},
            'tooltip': {'trigger': 'axis'},
            'dataZoom': [{'type': 'inside'}, {'type': 'slider', 'bottom': 10, 'height': 20}],
            'series': [],
        }
        if legend:
            options['legend'] = {'top': 32, 'type': 'scroll'}
        return options

    def refresh(self):
        if not hasattr(self, 'summary'):
            return
        result = self.result
        if result is None:
            self.export.disable()
            return
        try:
            stale = self.request()['key'] != result['key']
        except (ValueError, TypeError):
            stale = True
        s = result['summary']
        velocity = f"{s['velocity_m_s']:.3f} m/s" if s['valid'] else '无有效估计'
        snr = f"{s['snr_db']:.2f} dB" if s['snr_db'] is not None else '不可估计'
        self.summary.text = f"{'【已过期，请重算】 ' if stale else ''}风速：{velocity}　|　测风频谱 SNR：{snr}"
        src, p = result['source'], result['settings']
        error = f"{s['error_m_s']:.3f} m/s" if s['error_m_s'] is not None else '—'
        self.detail.text = (f"{self._result_reason(s)}；设定速度 {s['truth_m_s']:g} m/s；误差 {error}\n"
                            f"来源：{src['run_id'] or '当前结果'} / {SCENES[src['scene']]}；取样距离 {src['actual_range_m']:g} m；"
                            f"无噪声功率 {src['signal_power_w']:.4g} W\n"
                            f"单脉冲频率尺度 {result['diagnostics']['frequency_resolution_hz']/1e6:.3g} MHz")
        marks = [{'xAxis': p['reference_mhz'], 'name': '参考频率'}]
        if s['peak_hz'] is not None:
            marks.append({'xAxis': s['peak_hz']/1e6, 'name': '检出峰'})
        self.spectrum.options.update(
            series=[{'type': 'line', 'showSymbol': False,
                     'data': [[f/1e6, power] for f, power in zip(result['spectrum']['frequency_hz'], result['spectrum']['power'])],
                     'name': '事件谱', 'markLine': {'symbol': 'none', 'data': marks}},
                    {'type': 'line', 'showSymbol': False, 'name': '无拍频探测器噪声参考',
                     'lineStyle': {'type': 'dashed'},
                     'data': [[f/1e6, power] for f, power in zip(result['spectrum']['frequency_hz'], result['spectrum']['noise_reference'])]}],
            legend={'top': 32, 'type': 'scroll'})
        self.histogram.options.update(
            series=[{'type': 'bar', 'data': list(map(list, zip(result['histogram']['lag_ns'], result['histogram']['counts'])))}])
        self.spectrum.update()
        self.histogram.update()
        self.export.enable()

    @staticmethod
    def _result_reason(summary):
        return summary['reason'].replace('（理想单距离模型）', '').replace('（需超过 6 倍标准误差）', '')

    def download(self):
        if self.result:
            ui.download(json.dumps(self.result, ensure_ascii=False, indent=2).encode('utf-8'), 'wind_result.json')

    def cancel(self, *_args):
        self.cancelled = True
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()

    async def run(self):
        if self.running:
            return
        self.running, self.cancelled = True, False
        self.run_button.disable()
        self.cancel_button.enable()
        try:
            request = self.request()
            folder = self.root / 'temp/lidar_1d/wind_results' / request['key']
            folder.mkdir(parents=True, exist_ok=True)
            # Unique request/output files also isolate two browser clients.
            token = uuid.uuid4().hex
            request_path, output = folder / f'{token}.request.json', folder / f'{token}.result.json'
            request_path.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            self.status.text = '测风运行中…'
            self.log.append(f"\n[wind] 来源 {request['source']['run_id']}；任务 {request['key']}\n")
            self.process = await asyncio.create_subprocess_exec(
                resolve_mie_python_executable(), '-u', str(self.root / 'src/wind_worker.py'),
                '--request', str(request_path), '--output', str(output), cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if self.cancelled:
                self.cancel()
            while line := await self.process.stdout.readline():
                message = line.decode('utf-8', errors='replace')
                self.log.append('[wind] ' + message)
                self.status.text = message.strip()
            code = await self.process.wait()
            if self.cancelled:
                self.status.text = '测风已取消；保留此前结果'
            elif code:
                raise RuntimeError('测风计算失败，请查看日志中的具体原因')
            else:
                self.result = json.loads(output.read_text(encoding='utf-8'))
                self.state['result'] = self.result
                self.status.text = '测风完成：' + self._result_reason(self.result['summary'])
                self.refresh()
        except Exception as exc:
            self.log.append(f'[wind] {exc}\n')
            self.status.text = str(exc)
        finally:
            if self.process is not None and self.process.returncode is None:
                self.process.terminate()
                await self.process.wait()
            self.process = None
            self.running = False
            self.run_button.enable()
            self.cancel_button.disable()
