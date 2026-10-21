"""只读本地首音结构扫描与保存帧回放，不创建控制器或访问网络。"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    return {str(path.relative_to(ROOT)): sha(path) for path in (ROOT / 'project_sekai').glob('*.py')}


def output_directory(value):
    directory = Path(value).resolve()
    if not directory.is_relative_to((ROOT / '.local').resolve()):
        raise ValueError('离线结果只能写入本仓库 .local 目录')
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def metadata(gesture, color):
    return {'kind': gesture.kind, 'color': color(gesture), 'critical': gesture.critical,
            'flick': gesture.flick, 'points': [asdict(point) for point in gesture.points]}


def make_anchor(cp, chart):
    return cp.StartAnchor(chart.first, minimum_samples=4, gold_tap_context_chart=chart,
                          simultaneous_gestures=cp.first_note_context(chart),
                          dense_following_gestures=cp.dense_first_note_context(chart),
                          gold_tap_slide_following_gestures=cp.gold_tap_slide_following_context(chart))


def scan(args, cp, repository):
    from project_sekai.sus_chart import parse_sus
    manifest = repository.root / 'manifest.json'
    before = sha(manifest)
    rows = []
    missing_difficulties = []
    for song_id, song in sorted(repository.songs.items()):
        for difficulty in ('easy', 'normal', 'hard', 'expert', 'master', 'append'):
            entry = song.get('charts', {}).get(difficulty)
            if entry is None:
                missing_difficulties.append({'song_id': song_id, 'difficulty': difficulty})
                continue
            row = {'song_id': song_id, 'title': song.get('title', ''), 'difficulty': difficulty,
                   'level': entry.get('play_level'), 'path': entry.get('path'), 'sha256': entry.get('sha256')}
            try:
                chart = parse_sus(repository.load_chart(song_id, difficulty))
                first = chart.first
                heads = [gesture for gesture in chart.gestures if abs(gesture.start - first.start) <= 1e-9]
                anchor = make_anchor(cp, chart)
                tracker = anchor.chart_bound_tracker
                row.update(verified=True, first=metadata(first, cp.first_note_color),
                           first_group=[metadata(head, cp.first_note_color) for head in heads],
                           contexts={'simultaneous': len(cp.first_note_context(chart)),
                                     'dense': len(cp.dense_first_note_context(chart)),
                                     'gold_tap': len(cp.gold_tap_following_context(chart)),
                                     'gold_tap_skip_blue': len(cp.gold_tap_following_context(chart, skip_blue_taps=True)),
                                     'gold_tap_slide': len(cp.gold_tap_slide_following_context(chart))},
                           trace_guard_active=anchor.trace_opening_head_guard,
                           trace_in_first_group=any(head.kind == 'trace' for head in heads))
                signature = [{key: value for key, value in metadata(head, cp.first_note_color).items() if key != 'points'}
                             | {'lane': head.points[0].lane, 'width': head.points[0].width,
                                'point_kind': head.points[0].kind} for head in heads]
                row['signature'] = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
                if anchor.trace_opening_head_guard:
                    if not tracker.groups or tracker.following_geometry is None or not tracker.trace_phase_context.patterns:
                        row['coverage'] = 'trace_candidate_metadata_rejected'
                    else:
                        row['coverage'] = 'trace_candidate_requires_actual_markers_and_unique_identity'
                    row['reason'] = tracker.status
                    row['competing_origins'] = len(tracker.following_geometry or ())
                elif first.kind == 'trace':
                    row['coverage'] = 'legacy_trace_visual_unverified'
                    row['reason'] = 'candidate_disabled_or_non_gold'
                else:
                    row['coverage'] = 'existing_tap_slide_path_visual_unverified'
                    row['reason'] = 'trace_member_not_selected_first' if row['trace_in_first_group'] else 'legacy_path'
            except Exception as error:
                row.update(verified=False, coverage='load_or_parse_error', reason=f'{type(error).__name__}: {error}')
            rows.append(row)
            if args.limit and len(rows) >= args.limit:
                break
        if args.limit and len(rows) >= args.limit:
            break
    after = sha(manifest)
    if after != before:
        raise RuntimeError('扫描期间本地索引发生变化，结果作废')
    return {'manifest_sha256_before': before, 'manifest_sha256_after': after, 'rows': rows,
            'charts': len(rows), 'songs': len({row['song_id'] for row in rows}),
            'verified': sum(row['verified'] for row in rows), 'coverage_counts': dict(Counter(row['coverage'] for row in rows)),
            'unlisted_difficulties': missing_difficulties,
            'notice': '结构或哈希校验不是画面识别通过，候选资格不代表可安全播种，未标记不代表安全。'}


def replay(args, cp, repository):
    import cv2
    import numpy as np
    from project_sekai.sus_chart import parse_sus
    path = args.report.resolve()
    report_body = path.read_bytes()
    inputs = {str(path): hashlib.sha256(report_body).hexdigest()}
    report = json.loads(report_body.decode('utf-8-sig'))
    info = report['chart']
    song_id, difficulty = info['music_id'], info['difficulty']
    copied = path.parent / 'chart.sus'
    if copied.is_file():
        body = copied.read_bytes()
        inputs[str(copied)] = hashlib.sha256(body).hexdigest()
        if inputs[str(copied)] != info['sha256']:
            raise ValueError('保存谱面与本轮报告SHA256不一致')
        chart = parse_sus(body.decode('utf-8-sig'))
    else:
        chart = parse_sus(repository.load_chart(song_id, difficulty))
        if repository.songs[song_id]['charts'][difficulty]['sha256'] != info['sha256']:
            raise ValueError('当前曲库与历史报告不是同一谱面')
        chart_path = (repository.root / repository.songs[song_id]['charts'][difficulty]['path']).resolve()
        inputs[str(chart_path)] = info['sha256']
    frames = {value['captured_at']: value for value in report.get('anchor_frames', [])}
    for frame in report.get('opening_evidence', {}).get('frames', []):
        if frame.get('path'):
            frames.setdefault(frame['captured_at'], frame)
    if not 1 <= len(frames) <= 256:
        raise ValueError('缺少保存帧或保存帧超出256帧有界回放上限')
    verified = []
    for when, item in sorted(frames.items()):
        frame_path = (path.parent / item['path']).resolve()
        if not frame_path.is_relative_to(path.parent) or not frame_path.is_file():
            raise ValueError('保存帧缺失或路径越出报告目录，不能补认历史帧')
        body = frame_path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        inputs[str(frame_path)] = digest
        verified.append((when, item, frame_path, digest, body))
    anchor = make_anchor(cp, chart)
    result = {'report': str(path), 'report_sha256': inputs[str(path)], 'chart_sha256': info['sha256'],
              'song_id': song_id, 'difficulty': difficulty, 'observations': [], 'epoch': None,
              'frozen_anchor_flags': {key: value for key, value in vars(anchor).items() if type(value) is bool},
              'notice': '保留帧可能已覆盖真正首组；离线拟合不代表当前80ms交接或实机成功。'}
    for when, item, frame_path, digest, body in verified:
        frame = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape != (720, 1280, 3):
            raise ValueError('保存帧不可解码或不为1280x720')
        started = time.perf_counter()
        row = {'captured_at': when, 'path': str(frame_path), 'sha256': digest}
        try:
            epoch = anchor.observe(frame, when, capture_seconds=item.get('capture_seconds', 0.))
            result['epoch'] = None if epoch is None else float(epoch)
        except Exception as error:
            result['error'] = f'{type(error).__name__}: {error}'
        row.update(cost_ms=(time.perf_counter() - started) * 1000,
                   observation=anchor.observation, sample_count=len(anchor.samples))
        result['observations'].append(row)
        if result['epoch'] is not None or result.get('error'):
            break
    result.update(samples=anchor.samples, fit=anchor.fit, failure_reason=anchor.failure_reason)
    if any(not Path(name).is_file() or sha(Path(name)) != digest for name, digest in inputs.items()):
        raise RuntimeError('回放期间报告、谱面或输入帧变化，结果作废')
    result['input_sha256_before_and_after'] = inputs
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chart-root', type=Path, default=ROOT / 'resource/charts')
    parser.add_argument('--output', required=True, type=output_directory)
    parser.add_argument('--trace-guard', choices=('0', '1'), default='0')
    sub = parser.add_subparsers(dest='command', required=True)
    scan_parser = sub.add_parser('scan')
    scan_parser.add_argument('--limit', type=int, default=0)
    replay_parser = sub.add_parser('replay')
    replay_parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    if getattr(args, 'limit', 0) < 0:
        parser.error('--limit不能为负数')
    # 每次独立进程冻结候选，显式关闭旧实验，避免把环境串扰当新候选效果。
    flags = {'MAAPJSK_TRACE_OPENING_HEAD_GUARD': args.trace_guard,
             'MAAPJSK_CHART_BOUND_HEAD_GUARD': '0', 'MAAPJSK_GOLD_TAP_BLUE_CONTEXT_GUARD': '0',
             'MAAPJSK_ERASED_SLIDE_GUARD': '0'}
    os.environ.update(flags)
    from project_sekai import chart_player as cp
    from project_sekai.chart_catalog import ChartRepository
    before = source_hashes()
    repository = ChartRepository(args.chart_root)
    result = scan(args, cp, repository) if args.command == 'scan' else replay(args, cp, repository)
    if before != source_hashes():
        raise RuntimeError('离线验证期间源码发生变化，结果作废')
    result.update(source_hashes=before, tool_sha256=sha(Path(__file__)), flags=flags,
                  active_environment={key: value for key, value in os.environ.items() if key.startswith('MAAPJSK_')},
                  start_anchor_version=cp.START_ANCHOR_VERSION, touch_plan_version=cp.TOUCH_PLAN_VERSION)
    write_json(args.output / f'{args.command}.json', result)
    if args.command == 'scan':
        with (args.output / 'scan.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=('song_id', 'title', 'difficulty', 'level', 'verified', 'coverage', 'reason', 'sha256', 'path'), extrasaction='ignore')
            writer.writeheader()
            writer.writerows(result['rows'])
    print(json.dumps({key: result[key] for key in ('charts', 'verified', 'coverage_counts', 'epoch', 'failure_reason') if key in result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
