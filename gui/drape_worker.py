"""Standalone draping worker.

Runs the full draping pipeline (box mesh -> warp simulation -> glb export) in a
separate process, on its MAIN thread. Why a separate process:

* The per-frame timeout in ``pygarment.meshgen.simulation`` is implemented with POSIX
  signals (``signal.alarm``), and signals only work in the main thread of a process.
  NOTE: ``max_frame_time`` must stay null -- with a timeout set SIGALRM aborts the
  frame and the cloth does not move at all (the result equals the input box mesh).
* OpenGL (pyrender) and matplotlib UV-texture generation create NSWindows, which macOS
  only allows on the main thread -- calling them from a NiceGUI worker thread aborts the
  process with "NSWindow should only be instantiated on the main thread!".
* A crash (or a kill) of the simulation can no longer take the web interface down.

A lock file guarantees that only one simulation runs at a time, even across several
browser tabs / GUI instances.

Usage:
    python gui/drape_worker.py <request.json>

Request JSON:
    cwd        -- repository root to run in
    name       -- garment name
    design     -- design parameters dict (as expected by MetaGarment)
    body_file  -- path to a saved body measurements yaml
    out_dir    -- where to put the results
    props_file -- simulation properties yaml
    render     -- render preview images with pyrender (bool, needs a display)
"""
import fcntl
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

RESULT_PREFIX = 'GARMENTCODE_RESULT '

# Progress reporting: the GUI polls this file to draw a progress bar
# (stage ranges are in percent of the whole draping)
STAGE_RANGES = {
    'box_mesh': (0.0, 10.0),
    'sim': (10.0, 85.0),
    'export': (85.0, 100.0),
    'done': (100.0, 100.0),
    'error': (0.0, 0.0),
}
_progress_path = None


def set_progress(stage, percent=None, note=''):
    """Write the current progress for the GUI to poll"""
    if _progress_path is None:
        return
    low, high = STAGE_RANGES.get(stage, (0.0, 100.0))
    value = low if percent is None else low + (high - low) * min(max(float(percent), 0.0), 100.0) / 100.0
    payload = dict(stage=stage, percent=round(value, 1), note=note, ts=time.time())
    tmp = _progress_path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(payload, f)
    os.replace(tmp, _progress_path)


class ProgressTee:
    """Mirror stdout into the progress file, picking up run_sim's progress bar"""

    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        self.stream.write(data)
        # run_sim prints '\rProgress: [####-----] 42.0%'
        match = re.search(r'Progress:[^\]]*\]\s*([0-9.]+)%', data)
        if match:
            set_progress('sim', float(match.group(1)))
        return len(data)

    def flush(self):
        self.stream.flush()


def emit(payload):
    print(RESULT_PREFIX + json.dumps(payload, default=str), flush=True)


def main():
    request = json.loads(Path(sys.argv[1]).read_text())

    # Report the progress so that the GUI can show a progress bar
    global _progress_path
    _progress_path = Path(request['cwd']) / 'tmp_gui' / 'drape_progress.json'
    _progress_path.parent.mkdir(parents=True, exist_ok=True)
    set_progress('box_mesh', 0, 'Generating the mesh')
    sys.stdout = ProgressTee(sys.stdout)

    root = Path(request['cwd'])
    sys.path.insert(0, str(root))
    import os
    os.chdir(root)

    out_dir = Path(request['out_dir'])
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- Only one simulation at a time, process-wide ----
    lock_path = root / 'tmp_gui' / 'drape.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        emit({'status': 'busy', 'error': 'Another draping is already running'})
        return 3

    try:
        from assets.bodies.body_params import BodyParameters
        from assets.garment_programs.meta_garment import MetaGarment
        import pygarment.data_config as data_config
        from pygarment.meshgen.boxmeshgen import BoxMesh
        from pygarment.meshgen.simulation import run_sim
        from pygarment.meshgen.sim_config import PathCofig
        import trimesh

        body = BodyParameters(request['body_file'])
        garment = MetaGarment(request['name'], body, request['design'])
        pattern = garment.assembly()

        folder = Path(pattern.serialize(
            out_dir, to_subfolder=True,
            with_3d=False, with_text=False, view_ids=False, with_printable=True))

        props = data_config.Properties(request['props_file'])
        props.set_section_stats(
            'sim', fails={}, sim_time={}, spf={}, fin_frame={},
            body_collisions={}, self_collisions={})
        props.set_section_stats('render', render_time={})

        spec = sorted(folder.glob('*_specification.json'))[0]
        gname = spec.stem.rpartition('_')[0]
        paths = PathCofig(
            in_element_path=folder, out_path=out_dir,
            in_name=gname, out_name=f'{gname}_3D',
            body_name=request.get('body_name', 'mean_all'),
            smpl_body=False, add_timestamp=False)

        box_mesh = BoxMesh(paths.in_g_spec, props['sim']['config']['resolution_scale'])
        box_mesh.load()
        box_mesh.serialize(
            paths, store_panels=False,
            uv_config=props['render']['config']['uv_texture'])

        set_progress('sim', 0, 'Simulating')
        run_sim(
            box_mesh.name, props, paths,
            save_v_norms=False,
            store_usd=False,   # NOTE: False for fast simulation
            optimize_storage=False,
            verbose=False,
            render=request.get('render', False))

        set_progress('export', 0, 'Exporting')
        props.serialize(paths.element_sim_props)

        mesh = trimesh.load_mesh(paths.g_sim)
        pbr_material = mesh.visual.material.to_pbr()
        pbr_material.doubleSided = True
        mesh.visual.material = pbr_material
        mesh.export(paths.g_sim_glb)

        set_progress('done', 100, 'Done')
        emit({
            'status': 'ok',
            'out_el': str(paths.out_el),
            'glb_name': paths.g_sim_glb.name,
            'frames': props['sim']['stats']['fin_frame'].get(box_mesh.name),
            'sim_time': round(props['sim']['stats']['sim_time'].get(box_mesh.name, -1), 1),
            'fails': props['sim']['stats']['fails'],
        })
        return 0

    except BaseException as e:
        traceback.print_exc()
        set_progress('error', 0, f'{type(e).__name__}: {e}')
        emit({'status': 'error', 'error': f'{type(e).__name__}: {e}'})
        return 1


if __name__ == '__main__':
    sys.exit(main())
