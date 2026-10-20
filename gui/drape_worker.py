"""Standalone draping worker.

Runs the full draping pipeline (box mesh -> warp simulation -> glb export) in a
separate process, on its MAIN thread. Why a separate process:

* The per-frame timeout in ``pygarment.meshgen.simulation`` is implemented with POSIX
  signals (``signal.alarm``), and signals only work in the main thread of a process.
  Measured on macOS/CPU: with the timeout enabled the simulation runs ~50x faster
  (0.3 fps -> 24 fps on the same design).
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
import sys
import traceback
from pathlib import Path

RESULT_PREFIX = 'GARMENTCODE_RESULT '


def emit(payload):
    print(RESULT_PREFIX + json.dumps(payload, default=str), flush=True)


def main():
    request = json.loads(Path(sys.argv[1]).read_text())

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

        run_sim(
            box_mesh.name, props, paths,
            save_v_norms=False,
            store_usd=False,   # NOTE: False for fast simulation
            optimize_storage=False,
            verbose=False,
            render=request.get('render', False))

        props.serialize(paths.element_sim_props)

        mesh = trimesh.load_mesh(paths.g_sim)
        pbr_material = mesh.visual.material.to_pbr()
        pbr_material.doubleSided = True
        mesh.visual.material = pbr_material
        mesh.export(paths.g_sim_glb)

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
        emit({'status': 'error', 'error': f'{type(e).__name__}: {e}'})
        return 1


if __name__ == '__main__':
    sys.exit(main())
