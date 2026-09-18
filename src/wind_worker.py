"""Run in the existing mie environment, never inside the GUI event loop."""
import argparse
import json
from pathlib import Path

from wind_io import ALGORITHM_VERSION, make_request
from wind_receiver import simulate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        request = json.loads(Path(args.request).read_text(encoding='utf-8'))
        expected = make_request(request['source'], request['settings'])
        if request.get('algorithm_version') != ALGORITHM_VERSION or request.get('key') != expected['key']:
            raise ValueError('请求版本或摘要不匹配')
        result = simulate(request['source'], request['settings'],
                          lambda done, total: print(f'测风进度 {done}/{total}', flush=True))
        result['key'] = request['key']
        output = Path(args.output)
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        temporary.replace(output)
        print(result['summary']['reason'], flush=True)
    except Exception as exc:
        print(f'测风失败：{exc}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
