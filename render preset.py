bl_info = {
    "name": "Render Preset Manager (.blend Library Browser)",
    "author": "ChatGPT",
    "version": (2, 5, 0),
    "blender": (4, 5, 0),
    "location": "Render Properties > Presets",
    "description": "Save current render settings into standalone .blend files and re‑apply from a designated presets folder via an in‑panel browser (Cycles & Eevee). No objects/collections are touched.",
    "category": "Render",
}

import bpy
from bpy.types import Operator, Panel, UIList, PropertyGroup
from bpy.props import (
    StringProperty, BoolProperty, EnumProperty, CollectionProperty, IntProperty
)
import os
import glob
import shutil
from datetime import datetime

# -----------------------------------------------------------------------------
# Utilities – safe capture/apply of render settings only
# -----------------------------------------------------------------------------

def _iter_props(idblock):
    """Yield (identifier, value, prop_def) for writable scalar-like RNA props.
    Skips POINTER/COLLECTION; guards ENUMs with empty dynamic items."""
    if idblock is None:
        return
    for prop in idblock.bl_rna.properties:
        if prop.identifier in {"rna_type"}:
            continue
        if prop.is_readonly:
            continue
        if prop.type in {"POINTER", "COLLECTION"}:
            continue
        # Guard ENUM props with dynamic/empty item lists
        if getattr(prop, 'type', None) == 'ENUM':
            try:
                items = [it.identifier for it in prop.enum_items]
            except Exception:
                items = []
            if not items:
                continue
        try:
            val = getattr(idblock, prop.identifier)
        except Exception:
            continue
        yield prop.identifier, val, prop


def _serialize_group(idblock) -> dict:
    data = {}
    for key, val, prop in _iter_props(idblock):
        if getattr(prop, 'type', None) == 'ENUM':
            try:
                items = {it.identifier for it in prop.enum_items}
            except Exception:
                items = set()
            if isinstance(val, str) and items and val not in items:
                continue
        try:
            data[key] = val
        except Exception:
            pass
    return data


def _capture_preset(scene: bpy.types.Scene) -> dict:
    R = scene.render
    preset = {
        "engine": R.engine,
        "render": _serialize_group(R),
        "image_settings": _serialize_group(R.image_settings),
        "view_settings": _serialize_group(scene.view_settings),
        "display_settings": _serialize_group(scene.display_settings),
        "cycles": _serialize_group(getattr(scene, "cycles", None)),
        "eevee": _serialize_group(getattr(scene, "eevee", None)),
    }
    return preset


def _apply_group(idblock, data: dict):
    if idblock is None or not isinstance(data, dict):
        return
    for key, val in data.items():
        if not hasattr(idblock, key):
            continue
        try:
            prop = idblock.bl_rna.properties.get(key)
        except Exception:
            prop = None
        if prop and getattr(prop, 'type', None) == 'ENUM':
            try:
                items = [it.identifier for it in prop.enum_items]
            except Exception:
                items = []
            if not items:
                continue
            if isinstance(val, str) and val not in items:
                continue
        try:
            setattr(idblock, key, val)
        except Exception:
            # Interdependent/engine-specific props may fail; ignore and continue
            pass


def _apply_preset(scene: bpy.types.Scene, preset: dict,
                  *, switch_engine=True, apply_render=True,
                  apply_color=True, apply_engine=True):
    if not preset:
        return
    engine = preset.get("engine")
    if switch_engine and engine:
        try:
            scene.render.engine = engine
        except Exception:
            pass
    if apply_render:
        _apply_group(scene.render, preset.get("render"))
        _apply_group(scene.render.image_settings, preset.get("image_settings"))
    if apply_color:
        _apply_group(scene.view_settings, preset.get("view_settings"))
        _apply_group(scene.display_settings, preset.get("display_settings"))
    if apply_engine:
        if scene.render.engine == 'CYCLES' and hasattr(scene, 'cycles'):
            _apply_group(scene.cycles, preset.get("cycles"))
        if scene.render.engine in {'BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'} and hasattr(scene, 'eevee'):
            _apply_group(scene.eevee, preset.get("eevee"))


# -----------------------------------------------------------------------------
# Presets folder + file list (Collection + UIList)
# -----------------------------------------------------------------------------

class RPM_PresetEntry(PropertyGroup):
    name: StringProperty()
    path: StringProperty(subtype='FILE_PATH')
    mtime: StringProperty()
    size: StringProperty()


class RPM_UL_preset_files(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=item.name, icon='FILE_BLEND')
            row.label(text=item.mtime)
            row.label(text=item.size)
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text=os.path.splitext(item.name)[0])


# -----------------------------------------------------------------------------
# Session properties (WindowManager) + helpers
# -----------------------------------------------------------------------------

def _get_presets_dir(context) -> str:
    wm = context.window_manager
    root = wm.rpm_presets_dir or "//render_presets"
    path = bpy.path.abspath(root)
    if not os.path.isdir(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
    return path


def _scan_presets_into_list(context):
    wm = context.window_manager
    d = _get_presets_dir(context)
    patt = wm.rpm_filter.strip().lower() if wm.rpm_filter else ""
    files = glob.glob(os.path.join(d, "*.blend"))
    entries = []
    for p in files:
        base = os.path.basename(p)
        if patt and patt not in base.lower():
            continue
        try:
            mtime_ts = os.path.getmtime(p)
            size_b = os.path.getsize(p)
        except Exception:
            mtime_ts = 0
            size_b = 0
        mtime_str = datetime.fromtimestamp(mtime_ts).strftime("%Y-%m-%d %H:%M") if mtime_ts else ""
        size_kb = f"{max(1, int(size_b/1024))} KB"
        entries.append((p, base, mtime_ts, mtime_str, size_kb))
    # sort
    reverse = (wm.rpm_sort == 'NEWEST')
    entries.sort(key=lambda x: x[2], reverse=reverse)

    # fill collection
    coll = wm.rpm_items
    coll.clear()
    for p, base, _ts, mstr, sz in entries:
        it = coll.add()
        it.name = base
        it.path = p
        it.mtime = mstr
        it.size = sz
    wm.rpm_items_index = min(max(0, wm.rpm_items_index), max(0, len(coll)-1))


def _engine_label(engine_id: str) -> str:
    if engine_id == 'CYCLES':
        return 'Cycles'
    if engine_id in {'BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'}:
        return 'Eevee'
    return engine_id


def _sanitize(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name)
    return safe.strip("._") or "untitled"


def _auto_filename(context) -> str:
    wm = context.window_manager
    scn = context.scene
    tpl = wm.rpm_auto_name
    date = datetime.now().strftime("%Y%m%d-%H%M%S")
    w, h = scn.render.resolution_x, scn.render.resolution_y
    fps = round(scn.render.fps / scn.render.fps_base) if scn.render.fps_base else scn.render.fps
    tokens = {
        'date': date,
        'scene': _sanitize(scn.name),
        'engine': _sanitize(_engine_label(scn.render.engine)),
        'w': str(w),
        'h': str(h),
        'fps': str(fps),
    }
    try:
        name = tpl.format(**tokens)
    except Exception:
        name = f"{date}_{_sanitize(scn.name)}_{_engine_label(scn.render.engine)}_{w}x{h}_{fps}fps"
    return _sanitize(name) + ".blend"


# -----------------------------------------------------------------------------
# Operators – Export / Apply / Delete / Rename / Duplicate / Re‑Write / Refresh / Open / Search
# -----------------------------------------------------------------------------

class RPM_OT_quick_export(Operator):
    bl_idname = "rpm.quick_export"
    bl_label = "Quick Export to Folder"
    bl_options = {'REGISTER'}

    def execute(self, context):
        d = _get_presets_dir(context)
        fname = _auto_filename(context)
        path = os.path.join(d, fname)
        # Auto-increment if exists
        base, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(path):
            path = f"{base}_{i}{ext}"
            i += 1
        # Build temp scene and write
        src = context.scene
        tmp = bpy.data.scenes.new(name="__RPM_PRESET__")
        try:
            _apply_preset(tmp, _capture_preset(src))
            try:
                tmp.world = None
            except Exception:
                pass
            blocks = set(); blocks.add(tmp)
            bpy.data.libraries.write(path, blocks)
        except Exception as e:
            self.report({'ERROR'}, f"Export failed: {e}")
            return {'CANCELLED'}
        finally:
            try:
                bpy.data.scenes.remove(tmp)
            except Exception:
                pass
        self.report({'INFO'}, f"Saved: {os.path.basename(path)}")
        _scan_presets_into_list(context)
        return {'FINISHED'}


class RPM_OT_apply_selected(Operator):
    bl_idname = "rpm.apply_selected"
    bl_label = "Apply Selected"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        wm = context.window_manager
        coll = wm.rpm_items
        idx = wm.rpm_items_index
        if not coll or idx < 0 or idx >= len(coll):
            self.report({'ERROR'}, "No preset selected.")
            return {'CANCELLED'}
        path = coll[idx].path
        if not os.path.exists(path):
            self.report({'ERROR'}, "Preset file not found.")
            return {'CANCELLED'}
        # Load, apply, cleanup (scene‑only)
        try:
            with bpy.data.libraries.load(path, link=False) as (data_from, data_to):
                if not data_from.scenes:
                    self.report({'ERROR'}, "No Scene datablock in preset file.")
                    return {'CANCELLED'}
                target_name = data_from.scenes[0]
                data_to.scenes = [target_name]
        except Exception as e:
            self.report({'ERROR'}, f"Load failed: {e}")
            return {'CANCELLED'}
        try:
            imported_scene = bpy.data.scenes.get(target_name)
            if not imported_scene:
                self.report({'ERROR'}, "Imported scene unavailable.")
                return {'CANCELLED'}
            preset = _capture_preset(imported_scene)
            _apply_preset(
                context.scene, preset,
                switch_engine=wm.rpm_switch_engine,
                apply_render=wm.rpm_apply_render,
                apply_color=wm.rpm_apply_color,
                apply_engine=wm.rpm_apply_engine,
            )
        finally:
            try:
                bpy.data.scenes.remove(imported_scene)
            except Exception:
                pass
        self.report({'INFO'}, f"Applied: {os.path.basename(path)}")
        return {'FINISHED'}


class RPM_OT_delete_selected(Operator):
    bl_idname = "rpm.delete_selected"
    bl_label = "Delete Selected"

    def execute(self, context):
        wm = context.window_manager
        coll = wm.rpm_items
        idx = wm.rpm_items_index
        if not coll or idx < 0 or idx >= len(coll):
            self.report({'ERROR'}, "No preset selected.")
            return {'CANCELLED'}
        path = coll[idx].path
        try:
            os.remove(path)
        except Exception as e:
            self.report({'ERROR'}, f"Delete failed: {e}")
            return {'CANCELLED'}
        _scan_presets_into_list(context)
        self.report({'INFO'}, f"Deleted: {os.path.basename(path)}")
        return {'FINISHED'}


class RPM_OT_rewrite_selected(Operator):
    bl_idname = "rpm.rewrite_selected"
    bl_label = "Re‑Write Selected"
    bl_description = "Overwrite the selected preset .blend with current render settings"
    bl_options = {'REGISTER'}

    confirm: BoolProperty(name="Confirm overwrite", default=True)

    def invoke(self, context, event):
        if self.confirm:
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        layout.label(text="This will overwrite the selected .blend file.")

    def execute(self, context):
        wm = context.window_manager
        coll = wm.rpm_items
        idx = wm.rpm_items_index
        if not coll or idx < 0 or idx >= len(coll):
            self.report({'ERROR'}, "No preset selected.")
            return {'CANCELLED'}
        path = coll[idx].path
        if not os.path.exists(path):
            self.report({'ERROR'}, "Preset file not found.")
            return {'CANCELLED'}
        # Build temp scene and write to same path
        src = context.scene
        tmp = bpy.data.scenes.new(name="__RPM_PRESET__")
        try:
            _apply_preset(tmp, _capture_preset(src))
            try:
                tmp.world = None
            except Exception:
                pass
            blocks = set(); blocks.add(tmp)
            bpy.data.libraries.write(path, blocks)
        except Exception as e:
            self.report({'ERROR'}, f"Re‑write failed: {e}")
            return {'CANCELLED'}
        finally:
            try:
                bpy.data.scenes.remove(tmp)
            except Exception:
                pass
        _scan_presets_into_list(context)
        self.report({'INFO'}, f"Overwritten: {os.path.basename(path)}")
        return {'FINISHED'}


class RPM_OT_rename_selected(Operator):
    bl_idname = "rpm.rename_selected"
    bl_label = "Rename Selected"

    new_name: StringProperty(name="New File Name", description="Without folder; may include .blend", default="")

    def invoke(self, context, event):
        wm = context.window_manager
        coll = wm.rpm_items
        idx = wm.rpm_items_index
        if not coll or idx < 0 or idx >= len(coll):
            self.report({'ERROR'}, "No preset selected.")
            return {'CANCELLED'}
        base = coll[idx].name
        self.new_name = base
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        wm = context.window_manager
        coll = wm.rpm_items
        idx = wm.rpm_items_index
        path = coll[idx].path
        folder = _get_presets_dir(context)
        name = self.new_name.strip() or coll[idx].name
        if not name.lower().endswith('.blend'):
            name += '.blend'
        new_path = os.path.join(folder, _sanitize(name))
        try:
            os.replace(path, new_path)
        except Exception as e:
            self.report({'ERROR'}, f"Rename failed: {e}")
            return {'CANCELLED'}
        _scan_presets_into_list(context)
        self.report({'INFO'}, "Renamed.")
        return {'FINISHED'}


class RPM_OT_duplicate_selected(Operator):
    bl_idname = "rpm.duplicate_selected"
    bl_label = "Duplicate Selected"

    def execute(self, context):
        wm = context.window_manager
        coll = wm.rpm_items
        idx = wm.rpm_items_index
        if not coll or idx < 0 or idx >= len(coll):
            self.report({'ERROR'}, "No preset selected.")
            return {'CANCELLED'}
        src = coll[idx].path
        base, ext = os.path.splitext(os.path.basename(src))
        dst = os.path.join(_get_presets_dir(context), f"{base}_copy{ext}")
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(_get_presets_dir(context), f"{base}_copy{i}{ext}")
            i += 1
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            self.report({'ERROR'}, f"Duplicate failed: {e}")
            return {'CANCELLED'}
        _scan_presets_into_list(context)
        self.report({'INFO'}, f"Duplicated as {os.path.basename(dst)}")
        return {'FINISHED'}


class RPM_OT_open_folder(Operator):
    bl_idname = "rpm.open_folder"
    bl_label = "Open Folder"

    def execute(self, context):
        d = _get_presets_dir(context)
        try:
            bpy.ops.wm.path_open(filepath=d)
        except Exception:
            import sys, subprocess
            if sys.platform.startswith("win"):
                os.startfile(d)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])
            else:
                subprocess.Popen(["xdg-open", d])
        return {'FINISHED'}


class RPM_OT_refresh_list(Operator):
    bl_idname = "rpm.refresh_list"
    bl_label = "Refresh List"

    def execute(self, context):
        _scan_presets_into_list(context)
        return {'FINISHED'}


class RPM_OT_search(Operator):
    bl_idname = "rpm.search"
    bl_label = "Search Presets"

    query: StringProperty(name="Search", description="Substring match on filename", default="")

    def invoke(self, context, event):
        # Prefill with current filter
        self.query = context.window_manager.rpm_filter
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        wm = context.window_manager
        wm.rpm_filter = self.query.strip()
        _scan_presets_into_list(context)
        # Select first match if exists
        if wm.rpm_items:
            wm.rpm_items_index = 0
        return {'FINISHED'}


class RPM_OT_clear_search(Operator):
    bl_idname = "rpm.clear_search"
    bl_label = "Clear Search"

    def execute(self, context):
        context.window_manager.rpm_filter = ""
        _scan_presets_into_list(context)
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# UI Panel
# -----------------------------------------------------------------------------

class RPM_PT_panel(Panel):
    bl_label = "Render Presets (.blend)"
    bl_idname = "RPM_PT_panel"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager

        # Folder + controls
        box = layout.box()
        row = box.row(align=True)
        row.prop(wm, "rpm_presets_dir", text="Folder")
        row.operator("rpm.open_folder", text="Open", icon='FILE_FOLDER')
        row.operator("rpm.refresh_list", text="Refresh", icon='FILE_REFRESH')
        if not os.path.isdir(bpy.path.abspath(wm.rpm_presets_dir)):
            box.label(text="Folder will be created automatically.", icon='INFO')

        # Search & sort
        row = layout.row(align=True)
        row.operator("rpm.search", text="Search", icon='VIEWZOOM')
        row.operator("rpm.clear_search", text="Clear", icon='PANEL_CLOSE')
        row.prop(wm, "rpm_sort", text="Sort")

        # Browser list
        row = layout.row()
        row.template_list("RPM_UL_preset_files", "", wm, "rpm_items", wm, "rpm_items_index", rows=7)
        col = row.column(align=True)
        col.operator("rpm.apply_selected", text="Apply", icon='CHECKMARK')
        col.separator()
        col.operator("rpm.duplicate_selected", text="Duplicate", icon='DUPLICATE')
        col.operator("rpm.rename_selected", text="Rename", icon='OUTLINER_DATA_FONT')
        col.operator("rpm.delete_selected", text="Delete", icon='TRASH')
        col.operator("rpm.rewrite_selected", text="Re‑Write", icon='FILE_TICK')

        # Apply options
        box = layout.box()
        box.label(text="Apply Options")
        row = box.row(align=True)
        row.prop(wm, "rpm_switch_engine", text="Switch Engine")
        row.prop(wm, "rpm_apply_engine", text="Engine‑specific")
        row = box.row(align=True)
        row.prop(wm, "rpm_apply_render", text="Render + Output")
        row.prop(wm, "rpm_apply_color", text="Color Mgmt")

        # Quick export
        box = layout.box()
        box.label(text="Export")
        row = box.row(align=True)
        row.operator("rpm.quick_export", text="Quick Export to Folder", icon='FILE_BLEND')
        row = box.row(align=True)
        row.prop(wm, "rpm_auto_name", text="Auto Name")
        box.label(text=f"Preview: {_auto_filename(context)}")

        # Info
        layout.box().label(text="Presets affect only render/color/engine settings; scene data stays untouched.", icon='INFO')


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------

classes = (
    RPM_PresetEntry,
    RPM_UL_preset_files,
    RPM_OT_quick_export,
    RPM_OT_apply_selected,
    RPM_OT_delete_selected,
    RPM_OT_rewrite_selected,
    RPM_OT_rename_selected,
    RPM_OT_duplicate_selected,
    RPM_OT_open_folder,
    RPM_OT_refresh_list,
    RPM_OT_search,
    RPM_OT_clear_search,
    RPM_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    wm = bpy.types.WindowManager
    wm.rpm_presets_dir = StringProperty(
        name="Presets Folder",
        description="Folder containing .blend render presets (relative paths supported)",
        subtype='DIR_PATH',
        default="//render_presets",
    )
    wm.rpm_filter = StringProperty(name="Filter", description="Substring match on filename", default="")
    wm.rpm_sort = EnumProperty(
        name="Sort",
        items=(('NEWEST', "Newest", "Newest first"), ('OLDEST', "Oldest", "Oldest first")),
        default='NEWEST',
    )
    wm.rpm_items = CollectionProperty(type=RPM_PresetEntry)
    wm.rpm_items_index = IntProperty(default=0)

    wm.rpm_switch_engine = BoolProperty(name="Switch Engine", default=True)
    wm.rpm_apply_render = BoolProperty(name="Render + Output", default=True)
    wm.rpm_apply_color = BoolProperty(name="Color Mgmt", default=True)
    wm.rpm_apply_engine = BoolProperty(name="Engine‑specific", default=True)

    wm.rpm_auto_name = StringProperty(
        name="Auto Name",
        description="Template tokens: {date} {scene} {engine} {w} {h} {fps}",
        default="{date}_{scene}_{engine}_{w}x{h}_{fps}fps",
    )

    # Initial scan
    try:
        _scan_presets_into_list(bpy.context)
    except Exception:
        pass


def unregister():
    # Remove properties
    wm = bpy.types.WindowManager
    for attr in (
        'rpm_presets_dir', 'rpm_filter', 'rpm_sort', 'rpm_items', 'rpm_items_index',
        'rpm_switch_engine', 'rpm_apply_render', 'rpm_apply_color', 'rpm_apply_engine',
        'rpm_auto_name',
    ):
        if hasattr(wm, attr):
            delattr(wm, attr)
    # Unregister classes
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
