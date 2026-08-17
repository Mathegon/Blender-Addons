bl_info = {
    "name": "GRILLEN v2.0.1",
    "author": "Claude",
    "version": (2, 0, 1),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Grillen",
    "description": (
        "High-poly to low-poly texture baker with cage generation and multi-source support. "
        "v1.9.7: Per-map labeled supersample sub-boxes. "
        "v1.9.8: Fixed skip-clean losing previously baked maps from material; "
        "renamed to 'Only Rebake Changed'."
    ),
    "category": "Render",
}

import bpy
import os
from bpy.props import (
    PointerProperty, FloatProperty, IntProperty,
    StringProperty, BoolProperty, EnumProperty,
    CollectionProperty,
)
from bpy.types import PropertyGroup, Panel, Operator, UIList


# ---------------------------------------------------------------------------
# Rotation update callback
# ---------------------------------------------------------------------------

def _cage_rotation_update(self, context):
    cage = self.low_poly
    if cage is None:
        return
    if any(m.type == 'SHRINKWRAP' for m in cage.modifiers):
        cage.rotation_euler = (
            self.cage_rotation_x,
            self.cage_rotation_y,
            self.cage_rotation_z,
        )


# ---------------------------------------------------------------------------
# Dirty-flag invalidation callbacks
# ---------------------------------------------------------------------------

def _invalidate_all_maps(self, context):
    self.normal_baked    = False
    self.ao_baked        = False
    self.diffuse_baked   = False
    self.roughness_baked = False
    self.metalness_baked = False
    self.alpha_baked     = False

def _invalidate_ao(self, context):
    self.ao_baked = False


# ---------------------------------------------------------------------------
# Per-item property groups
# ---------------------------------------------------------------------------

class BAKER_PG_HighPolyItem(PropertyGroup):
    obj: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        description="High-poly source mesh",
        poll=lambda self, o: o.type == 'MESH',
    )


class BAKER_PG_QueueItem(PropertyGroup):
    """One entry in the bake queue."""
    enabled: BoolProperty(name="Enabled", default=True)

    # Single source
    high_poly: PointerProperty(
        name="High-Poly",
        type=bpy.types.Object,
        poll=lambda self, o: o.type == 'MESH',
    )
    low_poly: PointerProperty(
        name="Low-Poly",
        type=bpy.types.Object,
        poll=lambda self, o: o.type == 'MESH',
    )
    prefix_override: StringProperty(
        name="Prefix",
        description="Output prefix for this queue item (blank = use global prefix)",
        default="",
    )


# ---------------------------------------------------------------------------
# Property group
# ---------------------------------------------------------------------------

class BAKER_PG_Settings(PropertyGroup):

    # ── Objects ─────────────────────────────────────────────────────────────
    high_poly: PointerProperty(
        name="High-Poly",
        type=bpy.types.Object,
        description="The detailed source object to bake FROM",
        poll=lambda self, obj: obj.type == 'MESH',
        update=_invalidate_all_maps,
    )

    high_poly_list: CollectionProperty(type=BAKER_PG_HighPolyItem)
    high_poly_list_index: IntProperty(name="Active Index", default=0)

    multi_source: BoolProperty(
        name="Multiple Sources",
        description="Bake from multiple high-poly objects onto one low-poly",
        default=False,
        update=_invalidate_all_maps,
    )

    low_poly: PointerProperty(
        name="Low-Poly",
        type=bpy.types.Object,
        description="The optimised target object to bake TO",
        poll=lambda self, obj: obj.type == 'MESH',
        update=_invalidate_all_maps,
    )

    # ── Maps ────────────────────────────────────────────────────────────────
    bake_normals:   BoolProperty(name="Normal Map",        default=True)
    bake_ao:        BoolProperty(name="Ambient Occlusion", default=True)
    bake_diffuse:   BoolProperty(name="Diffuse (Color)",   default=False)
    bake_roughness: BoolProperty(name="Roughness",         default=False)
    bake_metalness: BoolProperty(name="Metalness",         default=False)
    bake_alpha:     BoolProperty(name="Alpha",             default=False)

    alpha_mode: EnumProperty(
        name="Alpha Source",
        description="How the alpha map is generated",
        items=[
            ('GEOMETRY', "Geometry Presence",
             "White where high-poly geometry exists, black where rays pass through gaps. "
             "Best for nets, wires, perforated surfaces."),
            ('MATERIAL', "Material Alpha",
             "Read the Alpha value from the high-poly Principled BSDF. "
             "Best when the high-poly already has an alpha texture."),
            ('COMBINED', "Combined (Geometry × Material)",
             "Bakes both geometry presence and material alpha, then multiplies them. "
             "Use when the model has both physical holes AND transparent materials."),
        ],
        default='GEOMETRY',
    )

    alpha_supersample: BoolProperty(
        name="Supersample Alpha (2×)",
        description="Bake Alpha at 2× resolution then box-filter downsample for smoother edges. "
                    "Doubles Alpha bake time but gives true sub-pixel anti-aliasing on wire/net edges.",
        default=True,
    )

    normal_supersample: BoolProperty(
        name="Supersample Normal (2×)",
        description="Bake Normal map at 2× resolution then downsample for smoother detail. "
                    "Vectors are renormalized after downsampling to keep lighting correct.",
        default=False,
    )

    alpha_smooth_sigma: FloatProperty(
        name="Edge Smoothing",
        description="Gaussian blur radius applied after baking to soften stairstepped edges. "
                    "0 = no smoothing. 1.0–2.0 is usually good for wires.",
        default=1.2, min=0.0, soft_max=3.0,
    )

    resolution: EnumProperty(
        name="Resolution",
        items=[
            ('512',  "512 px",  ""),
            ('1024', "1024 px", ""),
            ('2048', "2048 px", ""),
            ('4096', "4096 px", ""),
        ],
        default='1024',
        update=_invalidate_all_maps,
    )

    # Dirty flags — set False when a map needs rebaking
    normal_baked:    BoolProperty(default=False)
    ao_baked:        BoolProperty(default=False)
    diffuse_baked:   BoolProperty(default=False)
    roughness_baked: BoolProperty(default=False)
    metalness_baked: BoolProperty(default=False)
    alpha_baked:     BoolProperty(default=False)

    skip_clean_maps: BoolProperty(
        name="Only Rebake Changed",
        description="Skip maps that haven't changed since the last bake. "
                    "A map is marked 'changed' when you modify resolution, swap objects, "
                    "or adjust ray settings. Saves time on partial re-bakes.",
        default=False,
    )

    # ── Margin ──────────────────────────────────────────────────────────────
    margin: IntProperty(
        name="Margin (px)",
        description="Edge bleed margin around UV islands",
        default=16, min=0, max=64,
    )

    margin_type: EnumProperty(
        name="Margin Type",
        description="How the margin is filled — Adjacent Faces avoids dark seams on AO",
        items=[
            ('ADJACENT_FACES', "Adjacent Faces", "Fill margin by extending adjacent face colours — best for AO"),
            ('EXTEND',         "Extend",         "Extend border pixels outward — Blender default"),
        ],
        default='ADJACENT_FACES',
    )

    # ── Ray casting ─────────────────────────────────────────────────────────
    cage_extrusion: FloatProperty(
        name="Cage Extrusion",
        description="Inflate the low-poly when casting rays toward the high-poly",
        default=0.02, min=0.0, soft_max=1.0, unit='LENGTH',
        update=_invalidate_ao,
    )

    max_ray_distance: FloatProperty(
        name="Max Ray Distance",
        description="Maximum ray distance (0 = unlimited)",
        default=0.0, min=0.0, soft_max=5.0, unit='LENGTH',
        update=_invalidate_ao,
    )

    auto_ray_cast: BoolProperty(
        name="Auto Ray Cast",
        description="Calculate Cage Extrusion and Max Ray Distance automatically",
        default=False,
    )

    # ── Options ─────────────────────────────────────────────────────────────
    overwrite_images: BoolProperty(
        name="Overwrite Images",
        description="Overwrite existing bake images instead of creating numbered duplicates",
        default=True,
    )

    preserve_materials: BoolProperty(
        name="Preserve Materials",
        description="Append the baked material instead of replacing slot 0",
        default=False,
    )

    use_generated_cage: BoolProperty(
        name="Use Generated Cage",
        description="Use the _Cage object generated for the high-poly as the bake cage",
        default=False,
    )

    auto_apply_scale: BoolProperty(
        name="Auto Apply Scale",
        description="Automatically apply scale on objects with non-unit scale before baking",
        default=False,
    )

    post_bake_preview: BoolProperty(
        name="Preview After Bake",
        description="Switch viewport to Material Preview mode after baking completes",
        default=True,
    )

    # ── Cage generator ──────────────────────────────────────────────────────
    cage_poly_size: FloatProperty(
        name="Polygon Size",
        description="Target edge length for cage subdivisions",
        default=0.1, min=0.001, soft_max=2.0, unit='LENGTH',
    )

    cage_offset: FloatProperty(
        name="Cage Offset",
        description="Expand the bounding box outward from the high-poly",
        default=0.05, min=0.0, soft_max=1.0, unit='LENGTH',
    )

    cage_rotation_x: FloatProperty(
        name="Rotation X", default=0.0,
        soft_min=-3.14159, soft_max=3.14159, unit='ROTATION',
        update=_cage_rotation_update,
    )
    cage_rotation_y: FloatProperty(
        name="Rotation Y", default=0.0,
        soft_min=-3.14159, soft_max=3.14159, unit='ROTATION',
        update=_cage_rotation_update,
    )
    cage_rotation_z: FloatProperty(
        name="Rotation Z", default=0.0,
        soft_min=-3.14159, soft_max=3.14159, unit='ROTATION',
        description="Rotate cage to hide UV seams",
        update=_cage_rotation_update,
    )

    # ── Output ──────────────────────────────────────────────────────────────
    output_dir: StringProperty(
        name="Output Folder",
        description="Folder for baked images (blank = temp dir)",
        default="", subtype='DIR_PATH',
    )

    prefix: StringProperty(
        name="File Prefix",
        description="Optional prefix for saved filenames",
        default="",
    )

    output_format: EnumProperty(
        name="Format",
        description="Image file format for baked textures",
        items=[
            ('PNG',      "PNG",  "Lossless, widely supported, 8/16-bit"),
            ('TARGA',    "TGA",  "Uncompressed, fast, 8-bit only"),
            ('OPEN_EXR', "EXR",  "HDR format, 16/32-bit float, best for linear data"),
        ],
        default='PNG',
    )

    output_depth: EnumProperty(
        name="Bit Depth",
        description="Color depth per channel",
        items=[
            ('8',  "8-bit",   "Standard, smallest files"),
            ('16', "16-bit",  "Higher precision, good for normals"),
            ('32', "32-bit",  "Full float (EXR only)"),
        ],
        default='8',
    )

    normal_convention: EnumProperty(
        name="Normal Convention",
        description="Normal map Y-axis convention",
        items=[
            ('OPENGL',  "OpenGL (Y+)",  "Green channel as-is — Blender, Maya, Unity default"),
            ('DIRECTX', "DirectX (Y-)", "Green channel inverted — Unreal, 3ds Max, DirectX"),
        ],
        default='OPENGL',
    )

    export_orm: BoolProperty(
        name="Pack ORM",
        description="Export an additional packed ORM texture: "
                    "R=Ambient Occlusion, G=Roughness, B=Metallic. "
                    "Common for Unreal Engine and optimised PBR pipelines.",
        default=False,
    )

    # ── Bake queue ──────────────────────────────────────────────────────────
    queue: CollectionProperty(type=BAKER_PG_QueueItem)
    queue_index: IntProperty(name="Active Queue Item", default=0)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _res(s):
    return int(s.resolution)


def _ensure_uv(obj):
    if not obj.data.uv_layers:
        obj.data.uv_layers.new(name="UVMap")
    return obj.data.uv_layers.active.name


def _make_image(name, res, is_data=False, overwrite=True):
    if name in bpy.data.images:
        if overwrite:
            img = bpy.data.images[name]
            # Detach from file BEFORE scaling — FILE source ignores scale()
            if img.source == 'FILE':
                img.source   = 'GENERATED'
                img.filepath = ''
            # Set colorspace BEFORE scale — setting it after resets size to original
            img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'
            img.scale(res, res)
            return img
        else:
            idx = 1
            while f"{name}_{idx:03d}" in bpy.data.images:
                idx += 1
            name = f"{name}_{idx:03d}"
    img = bpy.data.images.new(name, width=res, height=res, alpha=False, is_data=is_data)
    img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'
    return img


def _set_active_image_node(obj, img):
    if len(obj.material_slots) == 0:
        obj.data.materials.append(None)
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            mat = bpy.data.materials.new(name=obj.name + "_BakeMat")
            mat.use_nodes = True
            slot.material = mat
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        bake_node = next(
            (n for n in nodes if n.type == 'TEX_IMAGE' and n.label == '__bake_target__'),
            None
        )
        if bake_node is None:
            bake_node = nodes.new('ShaderNodeTexImage')
            bake_node.label = '__bake_target__'
            bake_node.location = (-300, 300)
        bake_node.image = img
        for n in nodes:
            n.select = False
        bake_node.select = True
        nodes.active = bake_node


def _save_image(img, output_dir, filename, fmt='PNG', depth='8'):
    """Save image to disk with the specified format and bit depth."""
    EXT = {'PNG': '.png', 'TARGA': '.tga', 'OPEN_EXR': '.exr'}
    folder = output_dir.strip() or bpy.app.tempdir
    os.makedirs(folder, exist_ok=True)

    ext  = EXT.get(fmt, '.png')
    path = os.path.join(folder, filename + ext)

    img.filepath_raw = path
    img.file_format  = fmt

    # Clamp bit depth to what the format supports
    if fmt == 'TARGA':
        depth = '8'
    elif fmt == 'PNG' and depth == '32':
        depth = '16'
    elif fmt == 'OPEN_EXR' and depth == '8':
        depth = '16'

    if hasattr(img, 'use_half_precision'):
        img.use_half_precision = (depth == '16' and fmt == 'OPEN_EXR')

    # Set color depth on the image file output settings
    scene = bpy.context.scene
    orig_format = scene.render.image_settings.file_format
    orig_depth  = scene.render.image_settings.color_depth
    scene.render.image_settings.file_format  = fmt
    scene.render.image_settings.color_depth  = depth
    img.save_render(path, scene=scene)
    scene.render.image_settings.file_format  = orig_format
    scene.render.image_settings.color_depth  = orig_depth

    return path


def _flip_normal_green_channel(img):
    """Invert the green (Y) channel for DirectX normal map convention."""
    import numpy as np
    w, h = img.size
    pixels = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    pixels[:, :, 1] = 1.0 - pixels[:, :, 1]  # flip green
    img.pixels = pixels.ravel().tolist()


def _pack_orm(ao_img, rough_img, metal_img, output_dir, filename, fmt='PNG', depth='8'):
    """
    Pack AO/Roughness/Metallic into a single RGB image:
      R = Ambient Occlusion
      G = Roughness
      B = Metallic
    Creates and saves a new image, returns the path.
    """
    import numpy as np

    # Use the first available image's resolution
    ref = ao_img or rough_img or metal_img
    if ref is None:
        return None
    w, h = ref.size

    orm = np.ones((h, w, 4), dtype=np.float32)

    def get_channel(img):
        if img is None:
            return np.ones((h, w), dtype=np.float32)
        p = np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4)
        if p.shape[:2] != (h, w):
            # Resolution mismatch — return white
            return np.ones((h, w), dtype=np.float32)
        return p[:, :, 0]  # use red channel (greyscale data)

    orm[:, :, 0] = get_channel(ao_img)     # R = AO
    orm[:, :, 1] = get_channel(rough_img)  # G = Roughness
    orm[:, :, 2] = get_channel(metal_img)  # B = Metallic
    orm[:, :, 3] = 1.0

    orm_img = bpy.data.images.new(filename, w, h, alpha=False, is_data=True)
    orm_img.colorspace_settings.name = 'Non-Color'
    orm_img.pixels = orm.ravel().tolist()

    path = _save_image(orm_img, output_dir, filename, fmt=fmt, depth=depth)
    return path


def _assign_baked_material(low_obj, mat, preserve_materials):
    if preserve_materials:
        if mat not in low_obj.data.materials[:]:
            low_obj.data.materials.append(mat)
    else:
        if low_obj.data.materials:
            low_obj.data.materials[0] = mat
        else:
            low_obj.data.materials.append(mat)


def _build_baked_material(low_obj, baked_images, preserve_materials):
    mat_name = low_obj.name + "_Baked"
    mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    # ── Layout grid ──────────────────────────────────────────
    # Column positions (X):
    TEX_X   = -600     # texture image nodes
    MID_X   = -200     # processing nodes (Normal Map, AO Mix)
    BSDF_X  =  200     # Principled BSDF
    OUT_X   =  500     # Material Output
    # Row spacing (Y):
    ROW_H   =  280     # vertical spacing between texture rows
    TEX_W   =  250     # node width for textures

    # Start row positions from top
    row_y = 300

    # ── Output + BSDF ────────────────────────────────────────
    out  = nodes.new('ShaderNodeOutputMaterial')
    out.location = (OUT_X, 0)

    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (BSDF_X, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    # ── Diffuse / Base Color ─────────────────────────────────
    diff_tex = None
    if 'DIFFUSE' in baked_images:
        diff_tex = nodes.new('ShaderNodeTexImage')
        diff_tex.location = (TEX_X, row_y)
        diff_tex.width    = TEX_W
        diff_tex.image    = baked_images['DIFFUSE']
        diff_tex.label    = 'Diffuse'
        row_y -= ROW_H

    # ── AO ───────────────────────────────────────────────────
    ao_tex = None
    if 'AO' in baked_images:
        ao_tex = nodes.new('ShaderNodeTexImage')
        ao_tex.location = (TEX_X, row_y)
        ao_tex.width    = TEX_W
        ao_tex.image    = baked_images['AO']
        if ao_tex.image.colorspace_settings.name != 'Non-Color': ao_tex.image.colorspace_settings.name = 'Non-Color'
        ao_tex.label    = 'AO'
        row_y -= ROW_H

    # Wire Base Color: Diffuse * AO, or Diffuse alone, or AO alone
    if diff_tex and ao_tex:
        mix = nodes.new('ShaderNodeMixRGB')
        mix.location    = (MID_X, diff_tex.location.y)
        mix.blend_type  = 'MULTIPLY'
        mix.inputs['Fac'].default_value = 1.0
        links.new(diff_tex.outputs['Color'], mix.inputs['Color1'])
        links.new(ao_tex.outputs['Color'],   mix.inputs['Color2'])
        links.new(mix.outputs['Color'],      bsdf.inputs['Base Color'])
    elif diff_tex:
        links.new(diff_tex.outputs['Color'], bsdf.inputs['Base Color'])
    elif ao_tex:
        links.new(ao_tex.outputs['Color'],   bsdf.inputs['Base Color'])

    # ── Metalness ────────────────────────────────────────────
    if 'METALNESS' in baked_images:
        tex = nodes.new('ShaderNodeTexImage')
        tex.location = (TEX_X, row_y)
        tex.width    = TEX_W
        tex.image    = baked_images['METALNESS']
        if tex.image.colorspace_settings.name != 'Non-Color': tex.image.colorspace_settings.name = 'Non-Color'
        tex.label    = 'Metalness'
        links.new(tex.outputs['Color'], bsdf.inputs['Metallic'])
        row_y -= ROW_H

    # ── Roughness ────────────────────────────────────────────
    if 'ROUGHNESS' in baked_images:
        tex = nodes.new('ShaderNodeTexImage')
        tex.location = (TEX_X, row_y)
        tex.width    = TEX_W
        tex.image    = baked_images['ROUGHNESS']
        if tex.image.colorspace_settings.name != 'Non-Color': tex.image.colorspace_settings.name = 'Non-Color'
        tex.label    = 'Roughness'
        links.new(tex.outputs['Color'], bsdf.inputs['Roughness'])
        row_y -= ROW_H

    # ── Normal Map ───────────────────────────────────────────
    if 'NORMAL' in baked_images:
        tex = nodes.new('ShaderNodeTexImage')
        tex.location = (TEX_X, row_y)
        tex.width    = TEX_W
        tex.image    = baked_images['NORMAL']
        if tex.image.colorspace_settings.name != 'Non-Color': tex.image.colorspace_settings.name = 'Non-Color'
        tex.label    = 'Normal Map'
        nm  = nodes.new('ShaderNodeNormalMap')
        nm.location  = (MID_X, row_y)
        links.new(tex.outputs['Color'], nm.inputs['Color'])
        links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])
        row_y -= ROW_H

    # ── Alpha ────────────────────────────────────────────────
    if 'ALPHA' in baked_images:
        tex = nodes.new('ShaderNodeTexImage')
        tex.location = (TEX_X, row_y)
        tex.width    = TEX_W
        tex.image    = baked_images['ALPHA']
        if tex.image.colorspace_settings.name != 'Non-Color': tex.image.colorspace_settings.name = 'Non-Color'
        tex.label    = 'Alpha'
        links.new(tex.outputs['Color'], bsdf.inputs['Alpha'])
        # Viewport alpha display
        mat.blend_method         = 'HASHED'
        mat.use_backface_culling = False
        if hasattr(mat, 'shadow_method'):
            mat.shadow_method = 'HASHED'
        else:
            mat.use_transparent_shadow = True
        row_y -= ROW_H

    # ── ORM (packed channel texture) ─────────────────────────
    if 'ORM' in baked_images:
        row_y -= 40  # extra gap to separate from individual maps

        orm_tex = nodes.new('ShaderNodeTexImage')
        orm_tex.location = (TEX_X, row_y)
        orm_tex.width    = TEX_W
        orm_tex.image    = baked_images['ORM']
        if orm_tex.image.colorspace_settings.name != 'Non-Color': orm_tex.image.colorspace_settings.name = 'Non-Color'
        orm_tex.label    = 'ORM (packed)'
        orm_tex.use_custom_color = True
        orm_tex.color = (0.45, 0.30, 0.15)  # brown/amber to distinguish

        sep = nodes.new('ShaderNodeSeparateColor')
        sep.location = (MID_X, row_y)
        sep.label    = 'Unpack ORM'
        links.new(orm_tex.outputs['Color'], sep.inputs['Color'])

        # R=AO → multiply with Base Color (same pattern as individual AO)
        if diff_tex or 'DIFFUSE' in baked_images:
            # Find the existing Base Color link to insert AO multiply
            ao_mix = nodes.new('ShaderNodeMixRGB')
            ao_mix.location   = (MID_X + 150, row_y + 60)
            ao_mix.blend_type = 'MULTIPLY'
            ao_mix.inputs['Fac'].default_value = 1.0
            ao_mix.label = 'AO Multiply'

            # Grab whatever is currently connected to Base Color
            base_link = None
            for lnk in list(links):
                if lnk.to_socket == bsdf.inputs['Base Color']:
                    base_link = lnk.from_socket
                    links.remove(lnk)
                    break

            if base_link:
                links.new(base_link, ao_mix.inputs['Color1'])
            else:
                ao_mix.inputs['Color1'].default_value = (0.8, 0.8, 0.8, 1.0)

            links.new(sep.outputs['Red'], ao_mix.inputs['Color2'])
            links.new(ao_mix.outputs['Color'], bsdf.inputs['Base Color'])

        # G=Roughness → override individual Roughness connection
        for lnk in list(links):
            if lnk.to_socket == bsdf.inputs['Roughness']:
                links.remove(lnk)
        links.new(sep.outputs['Green'], bsdf.inputs['Roughness'])

        # B=Metallic → override individual Metallic connection
        for lnk in list(links):
            if lnk.to_socket == bsdf.inputs['Metallic']:
                links.remove(lnk)
        links.new(sep.outputs['Blue'], bsdf.inputs['Metallic'])

        row_y -= ROW_H

    _assign_baked_material(low_obj, mat, preserve_materials)
    return mat


def _get_view3d_context(context):
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    return area, region
    return None, None


def _find_cage_for_high(high_obj):
    cage_name = high_obj.name + "_Cage"
    obj = bpy.data.objects.get(cage_name)
    if obj and any(m.type == 'SHRINKWRAP' for m in obj.modifiers):
        return obj
    return None


def _check_and_handle_scale(obj, auto_apply, context):
    """
    Returns True if scale is fine (unit or auto-applied).
    Returns False if scale is non-unit and auto_apply is off (caller should cancel).
    """
    if all(abs(sc - 1.0) <= 1e-4 for sc in obj.scale):
        return True
    if auto_apply:
        prev_active   = context.view_layer.objects.active
        prev_selected = list(context.selected_objects)
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.transform_apply(scale=True)
        bpy.ops.object.select_all(action='DESELECT')
        for o in prev_selected:
            try: o.select_set(True)
            except: pass
        try: context.view_layer.objects.active = prev_active
        except: pass
        return True
    return False


def _check_uv_overlaps(obj):
    """
    Returns (has_overlaps, overlap_count).
    Only flags real overlaps between non-adjacent faces.
    """
    import bmesh
    from mathutils.geometry import intersect_tri_tri_2d

    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    uv_layer = bm.loops.layers.uv.active

    if not uv_layer:
        bm.free()
        return False, 0

    tris = []
    for face in bm.faces:
        uvs = tuple(loop[uv_layer].uv.copy().freeze() for loop in face.loops)
        if len(uvs) == 3:
            tris.append(uvs)
    bm.free()

    # Subsample for performance
    MAX_TRIS = 2000
    if len(tris) > MAX_TRIS:
        step = len(tris) // MAX_TRIS
        tris = tris[::step]

    overlaps = 0
    n = len(tris)
    for i in range(n):
        for j in range(i + 1, n):
            shared = set(tris[i]) & set(tris[j])
            if shared:
                continue
            if intersect_tri_tri_2d(*tris[i], *tris[j]):
                overlaps += 1
                if overlaps > 10:
                    return True, overlaps
    return overlaps > 0, overlaps


def _set_material_preview(context):
    """Switch all VIEW_3D areas to Material Preview shading."""
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.spaces.active.shading.type = 'MATERIAL'


_TEMP_PREFIX = "__grillen_tmp__"


def _purge_temps(cage_name=None):
    """Remove leftover temp cage target objects.
    If cage_name is given, only remove the temp for that specific cage.
    Otherwise remove all temps (for backward compatibility).
    """
    if cage_name:
        target_name = _TEMP_PREFIX + cage_name
        for obj in [o for o in bpy.data.objects if o.name.startswith(target_name)]:
            bpy.data.objects.remove(obj, do_unlink=True)
    else:
        for obj in [o for o in bpy.data.objects if o.name.startswith(_TEMP_PREFIX)]:
            bpy.data.objects.remove(obj, do_unlink=True)


def _auto_ray_distances(context, high_objects, low_obj, max_samples=500):
    """
    Measure the surface gap between low-poly and high-poly using find_nearest.
    Returns (cage_extrusion, max_ray_distance) at p95 with 25% margin, or None.
    """
    from mathutils.bvhtree import BVHTree

    depsgraph = context.evaluated_depsgraph_get()
    bvh_trees = [
        BVHTree.FromObject(obj.evaluated_get(depsgraph), depsgraph)
        for obj in high_objects
    ]

    low_eval = low_obj.evaluated_get(depsgraph)
    verts    = low_eval.data.vertices
    step     = max(1, len(verts) // max_samples)
    mat      = low_eval.matrix_world

    distances = []
    for i in range(0, len(verts), step):
        wp = mat @ verts[i].co
        for bvh in bvh_trees:
            loc, _, _, dist = bvh.find_nearest(wp)
            if loc is not None:
                distances.append(dist)

    if not distances:
        return None

    distances.sort()
    p95   = distances[int(len(distances) * 0.95)]
    value = round(p95 * 1.25, 4)
    return value, value


def _auto_cage_offset(context, high_objects):
    """
    Estimate cage offset from the high-poly surface by sampling bbox corners.
    Returns offset value or None.
    """
    from mathutils.bvhtree import BVHTree
    import mathutils

    depsgraph = context.evaluated_depsgraph_get()

    all_dists = []
    for obj in high_objects:
        bvh = BVHTree.FromObject(obj.evaluated_get(depsgraph), depsgraph)
        mw  = obj.matrix_world
        for corner in obj.bound_box:
            wp = mw @ mathutils.Vector(corner)
            loc, _, _, dist = bvh.find_nearest(wp)
            if loc is not None:
                all_dists.append(dist)

    if not all_dists:
        return None

    # Use 10% of the minimum corner-to-surface distance
    return round(min(all_dists) * 0.1, 4)


def _estimate_cage_faces(high_objects, poly_size, offset):
    """Estimate face count for the cage without building it."""
    import mathutils

    if not high_objects:
        return None

    all_corners = []
    for obj in high_objects:
        mw = obj.matrix_world
        all_corners.extend([mw @ mathutils.Vector(c) for c in obj.bound_box])

    xs = [v.x for v in all_corners]
    ys = [v.y for v in all_corners]
    zs = [v.z for v in all_corners]

    sx = max(xs) - min(xs) + offset * 2
    sy = max(ys) - min(ys) + offset * 2
    sz = max(zs) - min(zs) + offset * 2

    ps = max(poly_size, 1e-4)
    def segs(l): return max(1, round(l / ps))
    nx = min(segs(sx), 512)
    ny = min(segs(sy), 512)
    nz = min(segs(sz), 512)

    return 2 * (nx * ny) + 2 * (nx * nz) + 2 * (ny * nz)


def _setup_diffuse_bake(high_objects):
    """
    For each material on each high-poly: temporarily wire the Base Color
    source → Emission → Material Output so we can bake it via EMIT.

    Handles node Groups by adding a temporary Color output to the group
    interface, wiring the internal Base Color source to it, then connecting
    it to an Emission node on the outside.

    Returns restore data for _restore_diffuse_bake.
    """
    restore_data = []

    for obj in high_objects:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or not mat.use_nodes:
                continue

            nodes = mat.node_tree.nodes
            links = mat.node_tree.links

            output = next(
                (n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output),
                None
            )
            if output is None:
                continue

            surface_input = output.inputs['Surface']
            saved_links   = [(lnk.from_node, lnk.from_socket)
                             for lnk in links if lnk.to_socket == surface_input]

            color_socket    = None   # outer socket to wire to Emission
            group_cleanup   = None   # data for removing temp group output

            # --- Strategy 1: direct Principled BSDF (no group) ---
            bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if bsdf:
                bc = bsdf.inputs.get('Base Color')
                if bc:
                    for lnk in links:
                        if lnk.to_socket == bc:
                            color_socket = lnk.from_socket
                            break
                    if color_socket is None:
                        # No connection — will use default color below
                        pass

            # --- Strategy 2: Group node — go inside and extract ---
            if color_socket is None:
                group = next((n for n in nodes if n.type == 'GROUP'), None)
                if group and group.node_tree:
                    inner_tree  = group.node_tree
                    inner_nodes = inner_tree.nodes
                    inner_links = inner_tree.links

                    inner_bsdf = next(
                        (n for n in inner_nodes if n.type == 'BSDF_PRINCIPLED'), None
                    )
                    inner_bc_source = None

                    if inner_bsdf:
                        inner_bc = inner_bsdf.inputs.get('Base Color')
                        if inner_bc:
                            for lnk in inner_links:
                                if lnk.to_socket == inner_bc:
                                    inner_bc_source = lnk.from_socket
                                    break

                    if inner_bc_source:
                        # Add a temporary Color output to the group
                        temp_socket = inner_tree.interface.new_socket(
                            name="__bake_color__",
                            in_out='OUTPUT',
                            socket_type='NodeSocketColor',
                        )
                        group_output = next(
                            (n for n in inner_nodes if n.type == 'GROUP_OUTPUT'), None
                        )
                        if group_output:
                            inner_tree.links.new(
                                inner_bc_source,
                                group_output.inputs['__bake_color__']
                            )
                            # The new output is now accessible on the group node outside
                            color_socket = group.outputs.get('__bake_color__')

                            group_cleanup = {
                                'inner_tree':   inner_tree,
                                'temp_socket':  temp_socket,
                                'group_output': group_output,
                            }

            # --- Build Emission node ---
            emit_node = nodes.new('ShaderNodeEmission')
            emit_node.label    = '__diffuse_bake__'
            emit_node.location = (output.location.x - 200, output.location.y - 200)

            if color_socket:
                links.new(color_socket, emit_node.inputs['Color'])
            else:
                # Fallback: use default Base Color value
                default_color = (0.8, 0.8, 0.8, 1.0)
                if bsdf:
                    bc = bsdf.inputs.get('Base Color')
                    if bc:
                        default_color = tuple(bc.default_value)
                emit_node.inputs['Color'].default_value = default_color

            emit_node.inputs['Strength'].default_value = 1.0
            links.new(emit_node.outputs['Emission'], surface_input)

            restore_data.append({
                'mat':            mat,
                'emit_node':      emit_node,
                'surface_input':  surface_input,
                'saved_links':    saved_links,
                'group_cleanup':  group_cleanup,
            })

    return restore_data


def _restore_diffuse_bake(restore_data):
    """Undo _setup_diffuse_bake."""
    for rd in restore_data:
        mat       = rd['mat']
        links     = mat.node_tree.links
        nodes     = mat.node_tree.nodes
        emit_node = rd['emit_node']
        surf_in   = rd['surface_input']

        for lnk in list(links):
            if lnk.to_socket == surf_in and lnk.from_node == emit_node:
                links.remove(lnk)
        for from_node, from_socket in rd['saved_links']:
            links.new(from_socket, surf_in)
        nodes.remove(emit_node)

        # Remove temporary group output if we created one
        gc = rd.get('group_cleanup')
        if gc:
            inner_tree   = gc['inner_tree']
            group_output = gc['group_output']
            temp_socket  = gc['temp_socket']

            # Remove the internal link to __bake_color__
            bake_input = group_output.inputs.get('__bake_color__')
            if bake_input:
                for lnk in list(inner_tree.links):
                    if lnk.to_socket == bake_input:
                        inner_tree.links.remove(lnk)

            # Remove the interface socket
            inner_tree.interface.remove(temp_socket)


def _setup_metalness_bake(high_objects):
    """
    For each material on each high-poly object: temporarily connect
    Metallic → Emission so we can bake it via EMIT.
    Handles node Groups by tunneling the internal Metallic value out.
    """
    restore_data = []

    for obj in high_objects:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or not mat.use_nodes:
                continue

            nodes = mat.node_tree.nodes
            links = mat.node_tree.links

            output = next(
                (n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output),
                None
            )
            if output is None:
                continue

            surface_input      = output.inputs['Surface']
            saved_surface_links = [
                (lnk.from_node, lnk.from_socket)
                for lnk in links if lnk.to_socket == surface_input
            ]

            metallic_socket  = None   # outer socket carrying the metallic value
            metallic_default = 0.0    # fallback constant
            group_cleanup    = None

            # --- Strategy 1: direct Principled BSDF ---
            bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
            if bsdf:
                met_in = bsdf.inputs.get('Metallic')
                if met_in:
                    metallic_default = met_in.default_value
                    for lnk in links:
                        if lnk.to_socket == met_in:
                            metallic_socket = lnk.from_socket
                            break

            # --- Strategy 2: Group node — go inside ---
            if metallic_socket is None and bsdf is None:
                group = next((n for n in nodes if n.type == 'GROUP'), None)
                if group and group.node_tree:
                    inner_tree  = group.node_tree
                    inner_bsdf  = next(
                        (n for n in inner_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None
                    )
                    if inner_bsdf:
                        inner_met = inner_bsdf.inputs.get('Metallic')
                        if inner_met:
                            metallic_default = inner_met.default_value

                            # Check if something is connected inside
                            inner_source = None
                            for lnk in inner_tree.links:
                                if lnk.to_socket == inner_met:
                                    inner_source = lnk.from_socket
                                    break

                            if inner_source:
                                # Tunnel out through a temp group output
                                temp_socket = inner_tree.interface.new_socket(
                                    name="__bake_metallic__",
                                    in_out='OUTPUT',
                                    socket_type='NodeSocketFloat',
                                )
                                group_output = next(
                                    (n for n in inner_tree.nodes if n.type == 'GROUP_OUTPUT'),
                                    None
                                )
                                if group_output:
                                    inner_tree.links.new(
                                        inner_source,
                                        group_output.inputs['__bake_metallic__']
                                    )
                                    metallic_socket = group.outputs.get('__bake_metallic__')
                                    group_cleanup = {
                                        'inner_tree':   inner_tree,
                                        'temp_socket':  temp_socket,
                                        'group_output': group_output,
                                    }

            # --- Build Emission node ---
            emit_node = nodes.new('ShaderNodeEmission')
            emit_node.label    = '__metalness_bake__'
            emit_node.location = (output.location.x - 200, output.location.y - 200)
            emit_node.inputs['Color'].default_value = (1, 1, 1, 1)

            if metallic_socket:
                links.new(metallic_socket, emit_node.inputs['Strength'])
            else:
                emit_node.inputs['Strength'].default_value = metallic_default

            links.new(emit_node.outputs['Emission'], surface_input)

            restore_data.append({
                'mat':                 mat,
                'emit_node':           emit_node,
                'surface_input':       surface_input,
                'saved_surface_links': saved_surface_links,
                'group_cleanup':       group_cleanup,
            })

    return restore_data


def _restore_metalness_bake(restore_data):
    """Undo the temporary node wiring set up by _setup_metalness_bake."""
    for rd in restore_data:
        mat        = rd['mat']
        links      = mat.node_tree.links
        nodes      = mat.node_tree.nodes
        emit_node  = rd['emit_node']
        surface_in = rd['surface_input']

        for lnk in list(links):
            if lnk.to_socket == surface_in and lnk.from_node == emit_node:
                links.remove(lnk)
        for from_node, from_socket in rd['saved_surface_links']:
            links.new(from_socket, surface_in)
        nodes.remove(emit_node)

        # Remove temporary group output if we created one
        gc = rd.get('group_cleanup')
        if gc:
            inner_tree   = gc['inner_tree']
            group_output = gc['group_output']
            temp_socket  = gc['temp_socket']
            bake_input = group_output.inputs.get('__bake_metallic__')
            if bake_input:
                for lnk in list(inner_tree.links):
                    if lnk.to_socket == bake_input:
                        inner_tree.links.remove(lnk)
            inner_tree.interface.remove(temp_socket)


def _setup_alpha_bake(high_objects, mode):
    """
    Temporarily set up materials for alpha baking via EMIT.

    GEOMETRY mode: replace every material with pure white emission so that
    any ray hit = white (opaque) and any ray miss = black (transparent).
    Best for nets, wires, perforated surfaces.

    MATERIAL mode: wire the Principled BSDF Alpha socket → Emission Strength
    to capture the existing alpha value via an EMIT bake.
    """
    restore_data = []

    for obj in high_objects:

        if mode == 'GEOMETRY':
            orig_mats = [slot.material for slot in obj.material_slots]

            white_mat = bpy.data.materials.new("__grillen_alpha_white__")
            white_mat.use_nodes = True
            wn = white_mat.node_tree.nodes
            wl = white_mat.node_tree.links
            wn.clear()
            emit = wn.new('ShaderNodeEmission')
            emit.inputs['Color'].default_value    = (1, 1, 1, 1)
            emit.inputs['Strength'].default_value = 1.0
            out = wn.new('ShaderNodeOutputMaterial')
            wl.new(emit.outputs['Emission'], out.inputs['Surface'])

            for slot in obj.material_slots:
                slot.material = white_mat

            restore_data.append({
                'mode':      'GEOMETRY',
                'obj':       obj,
                'orig_mats': orig_mats,
                'white_mat': white_mat,
            })

        else:  # MATERIAL
            for slot in obj.material_slots:
                mat = slot.material
                if mat is None or not mat.use_nodes:
                    continue

                nodes = mat.node_tree.nodes
                links = mat.node_tree.links

                output = next((n for n in nodes
                                if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
                if output is None:
                    continue

                surface_in       = output.inputs['Surface']
                saved_surf_links = [(lnk.from_node, lnk.from_socket)
                                    for lnk in links if lnk.to_socket == surface_in]

                alpha_socket     = None    # outer socket carrying the alpha value
                alpha_default    = 1.0
                group_cleanup    = None

                # Strategy 1: direct Principled BSDF
                bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
                if bsdf:
                    alpha_in = bsdf.inputs.get('Alpha')
                    if alpha_in:
                        alpha_default = alpha_in.default_value
                        for lnk in links:
                            if lnk.to_socket == alpha_in:
                                alpha_socket = lnk.from_socket
                                break

                # Strategy 2: Group node — go inside
                if alpha_socket is None and bsdf is None:
                    group = next((n for n in nodes if n.type == 'GROUP'), None)
                    if group and group.node_tree:
                        inner_tree = group.node_tree
                        inner_bsdf = next(
                            (n for n in inner_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None
                        )
                        if inner_bsdf:
                            inner_alpha = inner_bsdf.inputs.get('Alpha')
                            if inner_alpha:
                                alpha_default = inner_alpha.default_value

                                # Trace connected source inside group
                                inner_source = None
                                for lnk in inner_tree.links:
                                    if lnk.to_socket == inner_alpha:
                                        inner_source = lnk.from_socket
                                        break

                                if inner_source:
                                    temp_socket = inner_tree.interface.new_socket(
                                        name="__bake_alpha__",
                                        in_out='OUTPUT',
                                        socket_type='NodeSocketFloat',
                                    )
                                    group_output = next(
                                        (n for n in inner_tree.nodes if n.type == 'GROUP_OUTPUT'),
                                        None
                                    )
                                    if group_output:
                                        inner_tree.links.new(
                                            inner_source,
                                            group_output.inputs['__bake_alpha__']
                                        )
                                        alpha_socket = group.outputs.get('__bake_alpha__')
                                        group_cleanup = {
                                            'inner_tree':   inner_tree,
                                            'temp_socket':  temp_socket,
                                            'group_output': group_output,
                                        }

                # Build emission node
                emit_node = nodes.new('ShaderNodeEmission')
                emit_node.label    = '__alpha_bake__'
                emit_node.location = (output.location.x - 200, output.location.y - 200)
                emit_node.inputs['Color'].default_value = (1, 1, 1, 1)

                if alpha_socket:
                    links.new(alpha_socket, emit_node.inputs['Strength'])
                else:
                    emit_node.inputs['Strength'].default_value = alpha_default

                links.new(emit_node.outputs['Emission'], surface_in)

                restore_data.append({
                    'mode':             'MATERIAL',
                    'mat':              mat,
                    'emit_node':        emit_node,
                    'surface_input':    surface_in,
                    'saved_surf_links': saved_surf_links,
                    'group_cleanup':    group_cleanup,
                })

    return restore_data


def _restore_alpha_bake(restore_data):
    """Undo the temporary node wiring set up by _setup_alpha_bake."""
    mats_to_remove = []

    for rd in restore_data:
        if rd['mode'] == 'GEOMETRY':
            obj       = rd['obj']
            orig_mats = rd['orig_mats']
            for i, slot in enumerate(obj.material_slots):
                slot.material = orig_mats[i] if i < len(orig_mats) else None
            mats_to_remove.append(rd['white_mat'])

        else:
            mat       = rd['mat']
            nodes     = mat.node_tree.nodes
            links     = mat.node_tree.links
            emit_node = rd['emit_node']
            surf_in   = rd['surface_input']

            for lnk in list(links):
                if lnk.to_socket == surf_in and lnk.from_node == emit_node:
                    links.remove(lnk)
            for from_node, from_socket in rd['saved_surf_links']:
                links.new(from_socket, surf_in)
            nodes.remove(emit_node)

            # Remove temporary group output if we created one
            gc = rd.get('group_cleanup')
            if gc:
                inner_tree   = gc['inner_tree']
                group_output = gc['group_output']
                temp_socket  = gc['temp_socket']
                bake_input   = group_output.inputs.get('__bake_alpha__')
                if bake_input:
                    for lnk in list(inner_tree.links):
                        if lnk.to_socket == bake_input:
                            inner_tree.links.remove(lnk)
                inner_tree.interface.remove(temp_socket)

    for mat in mats_to_remove:
        bpy.data.materials.remove(mat)


def _smooth_alpha_image(img, sigma):
    """
    Apply a separable Gaussian blur to the alpha image (stored as greyscale).
    Modifies img in place. sigma=0 skips smoothing.
    """
    if sigma <= 0:
        return
    import numpy as np

    w, h = img.size
    pixels = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    alpha_ch = pixels[:, :, 0].copy()

    # 1D Gaussian kernel
    size = max(3, int(6 * sigma) | 1)
    k = np.arange(-(size // 2), size // 2 + 1, dtype=np.float32)
    kernel = np.exp(-k ** 2 / (2 * sigma ** 2)).astype(np.float32)
    kernel /= kernel.sum()

    # Separable convolution
    blurred = np.apply_along_axis(
        lambda row: np.convolve(row, kernel, mode='same'), 1, alpha_ch)
    blurred = np.apply_along_axis(
        lambda col: np.convolve(col, kernel, mode='same'), 0, blurred)
    blurred = np.clip(blurred, 0.0, 1.0)

    pixels[:, :, 0] = blurred
    pixels[:, :, 1] = blurred
    pixels[:, :, 2] = blurred
    pixels[:, :, 3] = 1.0
    img.pixels = blurred_pixels = pixels.ravel().tolist()
    img.pixels = blurred_pixels


def _downsample_2x(img_hi, target_name, renormalize=False):
    """
    Box-filter downsample img_hi by 2× into a new Non-Color image named target_name.
    If renormalize=True, treats RGB as an encoded XYZ normal (0..1 → -1..1),
    renormalizes the averaged vector to unit length, and re-encodes it —
    box-filter averaging otherwise shrinks normal vectors and flattens the map.
    Returns the new image.
    """
    import numpy as np

    w, h = img_hi.size
    w2, h2 = w // 2, h // 2

    pixels = np.array(img_hi.pixels[:], dtype=np.float32).reshape(h, w, 4)

    # Box filter: average 2×2 blocks
    ds = (
        pixels[0::2, 0::2, :] +
        pixels[1::2, 0::2, :] +
        pixels[0::2, 1::2, :] +
        pixels[1::2, 1::2, :]
    ) / 4.0

    if renormalize:
        # Decode RGB (0..1) → XYZ (-1..1), normalize, re-encode
        xyz = ds[:, :, :3] * 2.0 - 1.0
        length = np.linalg.norm(xyz, axis=2, keepdims=True)
        length = np.where(length < 1e-6, 1.0, length)  # avoid divide-by-zero
        xyz_normalized = xyz / length
        ds[:, :, :3] = (xyz_normalized + 1.0) * 0.5

    ds = np.clip(ds, 0.0, 1.0)

    out = bpy.data.images.new(target_name, w2, h2, alpha=False, is_data=True)
    out.colorspace_settings.name = 'Non-Color'
    out.pixels = ds.ravel().tolist()
    return out


def _smooth_normals_across_seams(obj):
    """
    Temporarily set custom split normals from vertex normals (fully smooth,
    ignoring UV seam splits). This prevents tangent-space discontinuities
    at UV seams that cause ridge artifacts in normal map bakes.
    Returns restore data or None.
    """
    mesh = obj.data

    # Evaluate to get the deformed mesh (shrinkwrap etc.)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval  = obj.evaluated_get(depsgraph)

    # Get smooth vertex normals from the evaluated mesh
    vert_normals = [v.normal.copy() for v in obj_eval.data.vertices]

    # Store whether custom normals were already set
    had_custom = mesh.has_custom_normals

    # Build per-loop normals from vertex normals (smooth across all edges)
    loop_normals = [vert_normals[loop.vertex_index] for loop in mesh.loops]

    # Apply custom split normals
    mesh.normals_split_custom_set(loop_normals)

    return {'mesh': mesh, 'had_custom': had_custom}


def _restore_normals(restore_data):
    """Remove the temporary custom split normals."""
    if restore_data is None:
        return
    mesh = restore_data['mesh']
    if not restore_data['had_custom']:
        # Clear custom normals if they weren't there before
        if hasattr(mesh, 'free_normals_split'):
            mesh.free_normals_split()
        # Clear custom normals by toggling auto smooth
        bpy.ops.object.mode_set(mode='OBJECT')


# ---------------------------------------------------------------------------
# Core bake logic (shared by single bake and queue)
# ---------------------------------------------------------------------------

def _do_bake(operator, context, highs, low, s, prefix, baked_images_out):
    """
    Run all enabled bake passes. Populates baked_images_out dict.
    Returns list of saved paths, or raises on error.
    Uses bake handlers for progress reporting.
    Respects dirty flags when s.skip_clean_maps is True.
    """
    view3d_area, view3d_region = _get_view3d_context(context)
    if view3d_area is None:
        raise RuntimeError("No 3D Viewport found — open a 3D Viewport and try again.")

    res      = _res(s)
    low_name = low.name
    _ensure_uv(low)

    # Temporarily unhide source objects — hidden objects can't be selected
    # for baking, which silently produces a black result.
    hidden_objects = []
    for obj in highs + [low]:
        if obj.hide_get():
            hidden_objects.append(obj)
            obj.hide_set(False)

    cage_obj = None
    if not s.multi_source and s.use_generated_cage:
        cage_obj = _find_cage_for_high(highs[0])

    cage_extrusion   = s.cage_extrusion
    max_ray_distance = s.max_ray_distance
    if s.auto_ray_cast and not cage_obj:
        result = _auto_ray_distances(context, highs, low)
        if result:
            cage_extrusion, max_ray_distance = result
            operator.report({'INFO'},
                f"Auto ray cast: extrusion={cage_extrusion:.4f}, "
                f"max_ray_dist={max_ray_distance:.4f}"
            )
        else:
            operator.report({'WARNING'},
                "Auto ray cast found no hits — using manual values."
            )

    dirty_map = {
        'NORMAL':     not s.normal_baked,
        'AO':         not s.ao_baked,
        'DIFFUSE':    not s.diffuse_baked,
        'ROUGHNESS':  not s.roughness_baked,
        'METALNESS':  not s.metalness_baked,
        'ALPHA':      not s.alpha_baked,
    }

    passes = []
    if s.bake_normals:   passes.append(('NORMAL',     prefix + low_name + "_Normal",    True))
    if s.bake_ao:        passes.append(('AO',         prefix + low_name + "_AO",        True))
    if s.bake_diffuse:   passes.append(('DIFFUSE',    prefix + low_name + "_Diffuse",   False))
    if s.bake_roughness: passes.append(('ROUGHNESS',  prefix + low_name + "_Roughness", True))
    if s.bake_metalness: passes.append(('METALNESS',  prefix + low_name + "_Metalness", True))
    if s.bake_alpha:     passes.append(('ALPHA',      prefix + low_name + "_Alpha",     True))

    # Filter out clean maps if requested
    if s.skip_clean_maps:
        skipped = [bt for bt, _, _ in passes if not dirty_map.get(bt, True)]
        passes  = [(bt, n, d) for bt, n, d in passes if dirty_map.get(bt, True)]
        for bt in skipped:
            operator.report({'INFO'}, f"Skipping {bt} (not dirty).")

    total       = len(passes)
    saved_paths = []

    # Progress via handler — fires once per bake call (per pass)
    _bake_state = {'current': 0, 'total': total}
    wm = context.window_manager
    wm.progress_begin(0, 100)

    def _pre_handler(scene, context=None):
        _bake_state['current'] += 1
        pct = int((_bake_state['current'] - 1) / max(_bake_state['total'], 1) * 100)
        wm.progress_update(pct)

    bpy.app.handlers.object_bake_pre.append(_pre_handler)

    try:
        for bake_type, img_name, is_data in passes:
            operator.report({'INFO'}, f"Baking {bake_type}…")

            # Supersample: bake at 2× resolution, downsample after.
            # Always delete any existing image of this name so we start clean —
            # avoids stale sizes and Blender auto-renaming the downsampled result.
            bake_res = res
            supersampling = (
                (bake_type == 'ALPHA'  and s.alpha_supersample) or
                (bake_type == 'NORMAL' and s.normal_supersample)
            )
            if supersampling:
                bake_res = res * 2
                if img_name in bpy.data.images:
                    bpy.data.images.remove(bpy.data.images[img_name])

            img = _make_image(img_name, bake_res, is_data=is_data, overwrite=s.overwrite_images)
            _set_active_image_node(low, img)

            bpy.ops.object.select_all(action='DESELECT')
            for h in highs:
                h.select_set(True)
            low.select_set(True)
            context.view_layer.objects.active = low

            pass_filter = set()

            # Special bake types: wire specific inputs → Emission, bake as EMIT
            metalness_restore    = None
            alpha_restore        = None
            diffuse_restore      = None
            normals_restore      = None
            actual_bake_type     = bake_type
            alpha_extrusion      = cage_extrusion
            alpha_max_ray_dist   = max_ray_distance

            if bake_type == 'NORMAL':
                # Smooth normals across UV seams to prevent tangent-space
                # ridge artifacts at seam boundaries
                normals_restore = _smooth_normals_across_seams(low)
            elif bake_type == 'DIFFUSE':
                diffuse_restore  = _setup_diffuse_bake(highs)
                actual_bake_type = 'EMIT'
            elif bake_type == 'METALNESS':
                metalness_restore = _setup_metalness_bake(highs)
                actual_bake_type  = 'EMIT'
            elif bake_type == 'ALPHA':
                actual_bake_type = 'EMIT'
                effective_mode   = s.alpha_mode

                if effective_mode == 'COMBINED':
                    # ── Two-pass combined alpha: GEOMETRY × MATERIAL ──
                    # Pass 1: Geometry presence
                    operator.report({'INFO'}, "Alpha combined: baking geometry pass...")
                    alpha_restore = _setup_alpha_bake(highs, 'GEOMETRY')

                    gap = _auto_ray_distances(context, highs, low, max_samples=200)
                    if gap:
                        alpha_extrusion    = gap[0]
                        alpha_max_ray_dist = gap[0] * 3.0
                    else:
                        alpha_extrusion    = max(cage_extrusion, 0.02)
                        alpha_max_ray_dist = alpha_extrusion * 3.0

                    geo_img = _make_image(img_name + "__geo__", bake_res, is_data=True, overwrite=True)
                    _set_active_image_node(low, geo_img)

                    bpy.ops.object.select_all(action='DESELECT')
                    for h in highs: h.select_set(True)
                    low.select_set(True)
                    context.view_layer.objects.active = low

                    geo_kwargs = dict(
                        type='EMIT', pass_filter=set(),
                        use_selected_to_active=True,
                        max_ray_distance=alpha_max_ray_dist,
                        width=bake_res, height=bake_res,
                        margin=0, margin_type=s.margin_type,
                        use_clear=True, target='IMAGE_TEXTURES',
                    )
                    if cage_obj:
                        geo_kwargs['use_cage']    = True
                        geo_kwargs['cage_object'] = cage_obj.name
                    else:
                        geo_kwargs['cage_extrusion'] = alpha_extrusion

                    try:
                        with context.temp_override(area=view3d_area, region=view3d_region):
                            bpy.ops.object.bake(**geo_kwargs)
                    finally:
                        _restore_alpha_bake(alpha_restore)
                        alpha_restore = None

                    # Pass 2: Material alpha
                    operator.report({'INFO'}, "Alpha combined: baking material pass...")
                    alpha_restore = _setup_alpha_bake(highs, 'MATERIAL')

                    mat_img = _make_image(img_name + "__mat__", bake_res, is_data=True, overwrite=True)
                    _set_active_image_node(low, mat_img)

                    bpy.ops.object.select_all(action='DESELECT')
                    for h in highs: h.select_set(True)
                    low.select_set(True)
                    context.view_layer.objects.active = low

                    mat_kwargs = dict(
                        type='EMIT', pass_filter=set(),
                        use_selected_to_active=True,
                        max_ray_distance=max_ray_distance,
                        width=bake_res, height=bake_res,
                        margin=0, margin_type=s.margin_type,
                        use_clear=True, target='IMAGE_TEXTURES',
                    )
                    if cage_obj:
                        mat_kwargs['use_cage']    = True
                        mat_kwargs['cage_object'] = cage_obj.name
                    else:
                        mat_kwargs['cage_extrusion'] = cage_extrusion

                    try:
                        with context.temp_override(area=view3d_area, region=view3d_region):
                            bpy.ops.object.bake(**mat_kwargs)
                    finally:
                        _restore_alpha_bake(alpha_restore)
                        alpha_restore = None

                    # Multiply the two passes into the final image
                    import numpy as np
                    geo_px = np.array(geo_img.pixels[:], dtype=np.float32).reshape(-1, 4)
                    mat_px = np.array(mat_img.pixels[:], dtype=np.float32).reshape(-1, 4)
                    combined = geo_px.copy()
                    combined[:, :3] = geo_px[:, :3] * mat_px[:, :3]
                    img.pixels = combined.ravel().tolist()

                    # Clean up temp images
                    bpy.data.images.remove(geo_img)
                    bpy.data.images.remove(mat_img)

                    operator.report({'INFO'}, "Alpha combined: geometry × material merged.")
                    # Skip the normal bake call below — already done
                    actual_bake_type = '__SKIP__'

                else:
                    # Single-mode alpha (GEOMETRY or MATERIAL)
                    alpha_restore = _setup_alpha_bake(highs, effective_mode)

                    if effective_mode == 'GEOMETRY':
                        gap = _auto_ray_distances(context, highs, low, max_samples=200)
                        if gap:
                            near_dist = gap[0]
                            alpha_extrusion    = near_dist
                            alpha_max_ray_dist = near_dist * 3.0
                            operator.report({'INFO'},
                                f"Alpha geometry: extrusion={alpha_extrusion:.4f}, "
                                f"max_ray_dist={alpha_max_ray_dist:.4f} "
                                f"(auto-limited to near face only)"
                            )
                        else:
                            alpha_extrusion    = max(cage_extrusion, 0.02)
                            alpha_max_ray_dist = alpha_extrusion * 3.0

            bake_kwargs = dict(
                type=actual_bake_type,
                pass_filter=pass_filter,
                use_selected_to_active=True,
                max_ray_distance=(alpha_max_ray_dist if bake_type == 'ALPHA' and s.alpha_mode in ('GEOMETRY',) else max_ray_distance),
                width=bake_res, height=bake_res,
                # Alpha: no margin — bleeding widens cutout edges.
                # Normal: EXTEND margin — ADJACENT_FACES averages normals across
                # UV seams which blurs sharp creases into soft gradients.
                margin=0 if bake_type == 'ALPHA' else s.margin,
                margin_type='EXTEND' if bake_type == 'NORMAL' else s.margin_type,
                use_clear=True,
                target='IMAGE_TEXTURES',
            )
            if cage_obj:
                bake_kwargs['use_cage']    = True
                bake_kwargs['cage_object'] = cage_obj.name
            else:
                bake_kwargs['cage_extrusion'] = (
                    alpha_extrusion if bake_type == 'ALPHA' else cage_extrusion
                )

            # COMBINED alpha already baked above — skip the regular bake call
            if actual_bake_type == '__SKIP__':
                pass
            else:
                try:
                    with context.temp_override(area=view3d_area, region=view3d_region):
                        bpy.ops.object.bake(**bake_kwargs)
                finally:
                    if normals_restore is not None:
                        _restore_normals(normals_restore)
                    if diffuse_restore is not None:
                        _restore_diffuse_bake(diffuse_restore)
                    if metalness_restore is not None:
                        _restore_metalness_bake(metalness_restore)
                    if alpha_restore is not None:
                        _restore_alpha_bake(alpha_restore)

            # Post-processing: supersample downsample (+ gaussian smooth for alpha)
            final_img = img
            if bake_type == 'ALPHA':
                if s.alpha_supersample:
                    operator.report({'INFO'}, "Alpha: downsampling 2× supersample...")
                    ds_img = _downsample_2x(img, img_name + "__ds__", renormalize=False)
                    bpy.data.images.remove(img)  # remove 2× image
                    ds_img.name = img_name        # rename to final name (now free)
                    final_img = ds_img
                    baked_images_out[bake_type] = final_img

                if s.alpha_smooth_sigma > 0:
                    operator.report({'INFO'},
                        f"Alpha: smoothing edges (sigma={s.alpha_smooth_sigma:.1f})...")
                    _smooth_alpha_image(final_img, s.alpha_smooth_sigma)

            elif bake_type == 'NORMAL' and s.normal_supersample:
                operator.report({'INFO'}, "Normal: downsampling 2× supersample...")
                ds_img = _downsample_2x(img, img_name + "__ds__", renormalize=True)
                bpy.data.images.remove(img)
                ds_img.name = img_name
                final_img = ds_img
                baked_images_out[bake_type] = final_img

            # Normal map: flip green channel for DirectX convention
            if bake_type == 'NORMAL' and s.normal_convention == 'DIRECTX':
                operator.report({'INFO'}, "Normal: flipping green channel for DirectX...")
                _flip_normal_green_channel(final_img)

            path = _save_image(final_img, s.output_dir, img_name,
                               fmt=s.output_format, depth=s.output_depth)
            saved_paths.append(path)
            baked_images_out[bake_type] = final_img
            operator.report({'INFO'}, f"Saved: {path}")

            # Mark map as clean
            if bake_type == 'NORMAL':      s.normal_baked    = True
            elif bake_type == 'AO':        s.ao_baked        = True
            elif bake_type == 'DIFFUSE':   s.diffuse_baked   = True
            elif bake_type == 'ROUGHNESS': s.roughness_baked = True
            elif bake_type == 'METALNESS': s.metalness_baked = True
            elif bake_type == 'ALPHA':     s.alpha_baked     = True

    finally:
        bpy.app.handlers.object_bake_pre.remove(_pre_handler)
        wm.progress_end()

        # Restore hidden state for objects we unhid
        for obj in hidden_objects:
            obj.hide_set(True)

    # ORM packed texture export
    if s.export_orm:
        ao_img    = baked_images_out.get('AO')
        rough_img = baked_images_out.get('ROUGHNESS')
        metal_img = baked_images_out.get('METALNESS')
        if ao_img or rough_img or metal_img:
            orm_name = prefix + low_name + "_ORM"
            orm_path = _pack_orm(
                ao_img, rough_img, metal_img,
                s.output_dir, orm_name,
                fmt=s.output_format, depth=s.output_depth,
            )
            if orm_path:
                saved_paths.append(orm_path)
                operator.report({'INFO'}, f"ORM packed: {orm_path}")
                # Store ORM image so _build_baked_material can wire it
                orm_img = bpy.data.images.get(orm_name)
                if orm_img:
                    baked_images_out['ORM'] = orm_img

    return saved_paths


# ---------------------------------------------------------------------------
# High-poly list operators
# ---------------------------------------------------------------------------

class BAKER_UL_HighPolyList(UIList):
    bl_idname = "BAKER_UL_high_poly_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_prop):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            if item.obj:
                layout.prop(item, "obj", text="", emboss=False, icon='MESH_ICOSPHERE')
            else:
                layout.label(text="(empty)", icon='ERROR')


class BAKER_OT_HighPolyAdd(Operator):
    bl_idname  = "baker.high_poly_add"
    bl_label   = "Add High-Poly"
    bl_description = "Add all selected mesh objects as high-poly sources"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s          = context.scene.baker_settings
        candidates = [o for o in context.selected_objects if o.type == 'MESH']
        if not candidates:
            self.report({'WARNING'}, "Select at least one mesh object first.")
            return {'CANCELLED'}
        existing = {item.obj for item in s.high_poly_list}
        added = 0
        for obj in candidates:
            if obj in existing:
                continue
            item = s.high_poly_list.add()
            item.obj = obj
            item.name = obj.name
            added += 1
        if added == 0:
            self.report({'WARNING'}, "All selected objects are already in the list.")
            return {'CANCELLED'}
        s.high_poly_list_index = len(s.high_poly_list) - 1
        _invalidate_all_maps(s, context)
        self.report({'INFO'}, f"Added {added} object(s).")
        return {'FINISHED'}


class BAKER_OT_HighPolyRemove(Operator):
    bl_idname  = "baker.high_poly_remove"
    bl_label   = "Remove High-Poly"
    bl_description = "Remove the selected entry from the high-poly list"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s   = context.scene.baker_settings
        idx = s.high_poly_list_index
        if not s.high_poly_list:
            return {'CANCELLED'}
        s.high_poly_list.remove(idx)
        s.high_poly_list_index = max(0, idx - 1)
        _invalidate_all_maps(s, context)
        return {'FINISHED'}


class BAKER_OT_HighPolyClear(Operator):
    bl_idname  = "baker.high_poly_clear"
    bl_label   = "Clear High-Poly List"
    bl_description = "Remove all entries from the high-poly source list"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        context.scene.baker_settings.high_poly_list.clear()
        _invalidate_all_maps(context.scene.baker_settings, context)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Bake queue operators
# ---------------------------------------------------------------------------

class BAKER_UL_QueueList(UIList):
    bl_idname = "BAKER_UL_queue_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_prop):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="")
            high_name = item.high_poly.name if item.high_poly else "—"
            low_name  = item.low_poly.name  if item.low_poly  else "—"
            row.label(text=f"{high_name}  →  {low_name}", icon='RENDER_STILL')


class BAKER_OT_QueueAdd(Operator):
    bl_idname  = "baker.queue_add"
    bl_label   = "Add to Queue"
    bl_description = "Add the current High-Poly / Low-Poly pair to the bake queue"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s    = context.scene.baker_settings
        item = s.queue.add()
        item.high_poly = s.high_poly
        item.low_poly  = s.low_poly
        item.prefix_override = s.prefix
        s.queue_index = len(s.queue) - 1
        return {'FINISHED'}


class BAKER_OT_QueueRemove(Operator):
    bl_idname  = "baker.queue_remove"
    bl_label   = "Remove from Queue"
    bl_description = "Remove the selected queue entry"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s   = context.scene.baker_settings
        idx = s.queue_index
        if not s.queue:
            return {'CANCELLED'}
        s.queue.remove(idx)
        s.queue_index = max(0, idx - 1)
        return {'FINISHED'}


class BAKER_OT_QueueClear(Operator):
    bl_idname  = "baker.queue_clear"
    bl_label   = "Clear Queue"
    bl_description = "Remove all entries from the bake queue"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        context.scene.baker_settings.queue.clear()
        return {'FINISHED'}


class BAKER_OT_BakeQueue(Operator):
    bl_idname  = "baker.bake_queue"
    bl_label   = "Bake Queue"
    bl_description = "Bake all enabled queue items sequentially"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.baker_settings

        items = [item for item in s.queue if item.enabled]
        if not items:
            self.report({'ERROR'}, "No enabled items in the bake queue.")
            return {'CANCELLED'}

        if not (s.bake_normals or s.bake_ao or s.bake_diffuse or s.bake_roughness or s.bake_metalness or s.bake_alpha):
            self.report({'ERROR'}, "Enable at least one bake pass.")
            return {'CANCELLED'}

        view3d_area, _ = _get_view3d_context(context)
        if view3d_area is None:
            self.report({'ERROR'}, "No 3D Viewport found.")
            return {'CANCELLED'}

        orig_engine = context.scene.render.engine
        context.scene.render.engine = 'CYCLES'

        prev_active   = context.view_layer.objects.active
        prev_selected = list(context.selected_objects)

        success = 0
        for idx, item in enumerate(items):
            if item.high_poly is None or item.low_poly is None:
                self.report({'WARNING'}, f"Queue item {idx+1}: missing high or low poly — skipped.")
                continue

            highs = [item.high_poly]
            low   = item.low_poly

            if low in highs:
                self.report({'WARNING'}, f"Queue item {idx+1}: low-poly same as high-poly — skipped.")
                continue

            # Scale check
            scale_ok = True
            for obj in highs + [low]:
                if not _check_and_handle_scale(obj, s.auto_apply_scale, context):
                    self.report({'WARNING'},
                        f"Queue item {idx+1}: '{obj.name}' has unapplied scale — skipped.")
                    scale_ok = False
                    break
            if not scale_ok:
                continue

            # UV overlap check
            has_ov, ov_count = _check_uv_overlaps(low)
            if has_ov:
                self.report({'WARNING'},
                    f"Queue item {idx+1}: '{low.name}' has {ov_count}+ overlapping UV islands.")

            self.report({'INFO'}, f"Queue {idx+1}/{len(items)}: {item.high_poly.name} → {item.low_poly.name}")
            prefix = (item.prefix_override.strip() + "_") if item.prefix_override.strip() else (s.prefix.strip() + "_") if s.prefix.strip() else ""

            baked_images = {}

            # Pre-populate baked_images for skip_clean_maps
            if s.skip_clean_maps:
                low_name = low.name
                skip_lookup = {
                    'NORMAL':    ('normal_baked',    prefix + low_name + "_Normal"),
                    'AO':        ('ao_baked',        prefix + low_name + "_AO"),
                    'DIFFUSE':   ('diffuse_baked',   prefix + low_name + "_Diffuse"),
                    'ROUGHNESS': ('roughness_baked', prefix + low_name + "_Roughness"),
                    'METALNESS': ('metalness_baked', prefix + low_name + "_Metalness"),
                    'ALPHA':     ('alpha_baked',     prefix + low_name + "_Alpha"),
                }
                for bake_type, (flag_attr, img_name) in skip_lookup.items():
                    if getattr(s, flag_attr, False):
                        existing_img = bpy.data.images.get(img_name)
                        if existing_img:
                            baked_images[bake_type] = existing_img

            try:
                saved = _do_bake(self, context, highs, low, s, prefix, baked_images)
                mat   = _build_baked_material(low, baked_images, s.preserve_materials)
                self.report({'INFO'}, f"Queue {idx+1}: done — {len(saved)} map(s), material '{mat.name}'.")
                success += 1
            except Exception as e:
                self.report({'ERROR'}, f"Queue {idx+1} failed: {e}")

        context.scene.render.engine = orig_engine
        bpy.ops.object.select_all(action='DESELECT')
        for obj in prev_selected:
            try: obj.select_set(True)
            except: pass
        try: context.view_layer.objects.active = prev_active
        except: pass

        if s.post_bake_preview:
            _set_material_preview(context)

        self.report({'INFO'}, f"Queue done: {success}/{len(items)} items baked.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Main bake operator
# ---------------------------------------------------------------------------

class BAKER_OT_Bake(Operator):
    bl_idname  = "baker.bake"
    bl_label   = "Bake Now"
    bl_description = "Run high-poly → low-poly bake passes and build a material"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s   = context.scene.baker_settings
        low = s.low_poly

        if s.multi_source:
            highs = [item.obj for item in s.high_poly_list if item.obj is not None]
            if not highs:
                self.report({'ERROR'}, "Add at least one object to the high-poly list.")
                return {'CANCELLED'}
        else:
            if s.high_poly is None:
                self.report({'ERROR'}, "Please assign a High-Poly object.")
                return {'CANCELLED'}
            highs = [s.high_poly]

        if low is None:
            self.report({'ERROR'}, "Please assign a Low-Poly object.")
            return {'CANCELLED'}
        if low in highs:
            self.report({'ERROR'}, "Low-Poly cannot also be in the high-poly list.")
            return {'CANCELLED'}
        if not (s.bake_normals or s.bake_ao or s.bake_diffuse or s.bake_roughness or s.bake_metalness or s.bake_alpha):
            self.report({'ERROR'}, "Enable at least one bake pass.")
            return {'CANCELLED'}

        # Scale check
        for obj in highs + [low]:
            if not _check_and_handle_scale(obj, s.auto_apply_scale, context):
                self.report({'ERROR'},
                    f"'{obj.name}' has unapplied scale. Enable Auto Apply Scale or apply manually (Ctrl+A)."
                )
                return {'CANCELLED'}

        # UV overlap check
        has_ov, ov_count = _check_uv_overlaps(low)
        if has_ov:
            self.report({'WARNING'},
                f"'{low.name}' has {ov_count}+ overlapping UV islands — bake may be incorrect."
            )

        orig_engine = context.scene.render.engine
        context.scene.render.engine = 'CYCLES'

        prev_active   = context.view_layer.objects.active
        prev_selected = list(context.selected_objects)

        prefix       = (s.prefix.strip() + "_") if s.prefix.strip() else ""
        baked_images = {}

        # Pre-populate baked_images with existing images for any maps that
        # will be skipped (so _build_baked_material still wires them up)
        if s.skip_clean_maps:
            low_name = low.name
            skip_lookup = {
                'NORMAL':    ('normal_baked',    prefix + low_name + "_Normal"),
                'AO':        ('ao_baked',        prefix + low_name + "_AO"),
                'DIFFUSE':   ('diffuse_baked',   prefix + low_name + "_Diffuse"),
                'ROUGHNESS': ('roughness_baked', prefix + low_name + "_Roughness"),
                'METALNESS': ('metalness_baked', prefix + low_name + "_Metalness"),
                'ALPHA':     ('alpha_baked',     prefix + low_name + "_Alpha"),
            }
            for bake_type, (flag_attr, img_name) in skip_lookup.items():
                if getattr(s, flag_attr, False):
                    existing_img = bpy.data.images.get(img_name)
                    if existing_img:
                        baked_images[bake_type] = existing_img

        try:
            saved = _do_bake(self, context, highs, low, s, prefix, baked_images)
            mat   = _build_baked_material(low, baked_images, s.preserve_materials)
            self.report({'INFO'}, f"Material '{mat.name}' applied to '{low.name}'.")

        except Exception as e:
            self.report({'ERROR'}, f"Bake failed: {e}")
            return {'CANCELLED'}

        finally:
            context.scene.render.engine = orig_engine
            bpy.ops.object.select_all(action='DESELECT')
            for obj in prev_selected:
                try: obj.select_set(True)
                except: pass
            try: context.view_layer.objects.active = prev_active
            except: pass

        if s.post_bake_preview:
            _set_material_preview(context)

        src_desc = f"{len(highs)} objects" if s.multi_source else highs[0].name
        self.report({'INFO'},
            f"Done! Baked from {src_desc} → '{low.name}'. "
            f"{len(saved)} map(s) → {s.output_dir.strip() or bpy.app.tempdir}"
        )
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Auto cage offset operator
# ---------------------------------------------------------------------------

class BAKER_OT_BakeSelectedNode(Operator):
    bl_idname  = "baker.bake_selected_node"
    bl_label   = "Bake Selected Node"
    bl_description = (
        "Bake the output of the active node in the Shader Editor to a texture. "
        "Useful for flattening adjustment nodes (Curves, Hue/Sat, etc.) before glTF export."
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s   = context.scene.baker_settings
        obj = context.active_object

        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select a mesh object.")
            return {'CANCELLED'}

        if not obj.data.uv_layers:
            self.report({'ERROR'}, f"'{obj.name}' has no UV map.")
            return {'CANCELLED'}

        if not obj.data.materials or not obj.data.materials[0]:
            self.report({'ERROR'}, f"'{obj.name}' has no material.")
            return {'CANCELLED'}

        mat   = obj.data.materials[0]
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # Get active node
        source_node = nodes.active
        if source_node is None:
            self.report({'ERROR'}, "No active node — select a node in the Shader Editor.")
            return {'CANCELLED'}

        if source_node.type == 'OUTPUT_MATERIAL':
            self.report({'ERROR'}, "Cannot bake the Material Output node itself.")
            return {'CANCELLED'}

        # Find the best output socket — prefer Color/RGBA, then first VALUE
        source_socket = None
        for out in source_node.outputs:
            if out.type == 'RGBA':
                source_socket = out
                break
        if source_socket is None:
            for out in source_node.outputs:
                if out.type in ('VALUE', 'VECTOR'):
                    source_socket = out
                    break
        if source_socket is None:
            source_socket = source_node.outputs[0] if source_node.outputs else None

        if source_socket is None:
            self.report({'ERROR'}, f"Node '{source_node.name}' has no output sockets.")
            return {'CANCELLED'}

        view3d_area, view3d_region = _get_view3d_context(context)
        if view3d_area is None:
            self.report({'ERROR'}, "No 3D Viewport found.")
            return {'CANCELLED'}

        # Determine if result is color or data
        is_data = source_socket.type in ('VALUE', 'VECTOR')

        # Create image name from object + node
        node_label = source_node.label or source_node.name
        safe_name  = "".join(c if c.isalnum() or c in " _-" else "_" for c in node_label).strip()
        img_name   = f"{obj.name}_{safe_name}"

        res = _res(s)
        img = _make_image(img_name, res, is_data=is_data, overwrite=s.overwrite_images)

        # --- Temporarily rewire: source_socket → Emission → Material Output ---
        output_node = next(
            (n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output),
            None
        )
        if output_node is None:
            self.report({'ERROR'}, "Material has no active Material Output node.")
            return {'CANCELLED'}

        surface_input  = output_node.inputs['Surface']
        saved_links    = [(lnk.from_node, lnk.from_socket) for lnk in links if lnk.to_socket == surface_input]

        emit_node = nodes.new('ShaderNodeEmission')
        emit_node.label    = '__node_bake_tmp__'
        emit_node.location = (output_node.location.x - 200, output_node.location.y - 200)

        # Wire source → Emission Color (for RGBA) or Strength (for VALUE)
        if source_socket.type == 'RGBA':
            links.new(source_socket, emit_node.inputs['Color'])
            emit_node.inputs['Strength'].default_value = 1.0
        else:
            links.new(source_socket, emit_node.inputs['Strength'])
            emit_node.inputs['Color'].default_value = (1, 1, 1, 1)

        links.new(emit_node.outputs['Emission'], surface_input)

        # Set bake target image on material
        bake_tex = nodes.new('ShaderNodeTexImage')
        bake_tex.label    = '__bake_target__'
        bake_tex.image    = img
        bake_tex.location = (emit_node.location.x - 300, emit_node.location.y)
        for n in nodes:
            n.select = False
        bake_tex.select = True
        nodes.active = bake_tex

        # --- Bake EMIT (object's own material, no Selected-to-Active) ---
        orig_engine = context.scene.render.engine
        context.scene.render.engine = 'CYCLES'

        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        try:
            with context.temp_override(area=view3d_area, region=view3d_region):
                bpy.ops.object.bake(
                    type='EMIT',
                    use_selected_to_active=False,
                    width=res, height=res,
                    margin=s.margin,
                    margin_type=s.margin_type,
                    use_clear=True,
                    target='IMAGE_TEXTURES',
                )

            path = _save_image(img, s.output_dir, img_name,
                               fmt=s.output_format, depth=s.output_depth)
            self.report({'INFO'}, f"Baked node '{node_label}' → {path}")

        except Exception as e:
            self.report({'ERROR'}, f"Node bake failed: {e}")
            return {'CANCELLED'}

        finally:
            context.scene.render.engine = orig_engine

            # Restore material
            for lnk in list(links):
                if lnk.to_socket == surface_input and lnk.from_node == emit_node:
                    links.remove(lnk)
            for from_node, from_socket in saved_links:
                links.new(from_socket, surface_input)
            nodes.remove(emit_node)
            nodes.remove(bake_tex)

        # Insert the baked result as a new color-coded Image Texture node
        result_node = nodes.new('ShaderNodeTexImage')
        result_node.image    = img
        result_node.label    = f"Baked: {node_label}"
        result_node.name     = f"Baked_{safe_name}"
        result_node.location = (
            source_node.location.x,
            source_node.location.y - 250,
        )
        result_node.width = 200

        # Color-code: distinct green so baked nodes stand out
        result_node.use_custom_color = True
        result_node.color = (0.18, 0.55, 0.28)

        # Select only the new node so it's easy to find
        for n in nodes:
            n.select = False
        result_node.select = True
        nodes.active = result_node

        return {'FINISHED'}


class BAKER_OT_AutoCageOffset(Operator):
    bl_idname  = "baker.auto_cage_offset"
    bl_label   = "Auto Offset"
    bl_description = "Calculate cage offset from the high-poly surface"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.baker_settings
        if s.multi_source:
            sources = [item.obj for item in s.high_poly_list if item.obj]
        else:
            sources = [s.high_poly] if s.high_poly else []

        if not sources:
            self.report({'ERROR'}, "No high-poly source assigned.")
            return {'CANCELLED'}

        offset = _auto_cage_offset(context, sources)
        if offset is None:
            self.report({'WARNING'}, "Could not calculate offset — check object geometry.")
            return {'CANCELLED'}

        s.cage_offset = offset
        self.report({'INFO'}, f"Cage offset set to {offset:.4f} m.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Cage mesh generation
# ---------------------------------------------------------------------------

def _build_cage_bmesh(bbox_center, bbox_size, poly_size):
    import bmesh

    sx, sy, sz = bbox_size.x, bbox_size.y, bbox_size.z
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    ps = max(poly_size, 1e-4)

    def segs(length):
        return max(1, round(length / ps))

    nx, ny, nz = segs(sx), segs(sy), segs(sz)

    MAX_CAGE_SEGMENTS = 512
    MAX_CAGE_FACES    = 2_000_000
    nx = min(nx, MAX_CAGE_SEGMENTS)
    ny = min(ny, MAX_CAGE_SEGMENTS)
    nz = min(nz, MAX_CAGE_SEGMENTS)

    estimated_faces = (
        2 * (nx * ny) +
        2 * (nx * nz) +
        2 * (ny * nz)
    )
    if estimated_faces > MAX_CAGE_FACES:
        raise RuntimeError(
            f"Cage would generate {estimated_faces:,} faces "
            f"(limit {MAX_CAGE_FACES:,}). Increase Polygon Size."
        )

    bm = bmesh.new()

    r = bmesh.ops.create_grid(bm, x_segments=nx, y_segments=ny, size=1.0)
    for v in r['verts']:
        v.co.x *= hx;  v.co.y *= hy;  v.co.z = hz

    r = bmesh.ops.create_grid(bm, x_segments=nx, y_segments=ny, size=1.0)
    for v in r['verts']:
        v.co.x *= hx;  v.co.y = -v.co.y * hy;  v.co.z = -hz

    r = bmesh.ops.create_grid(bm, x_segments=ny, y_segments=nz, size=1.0)
    for v in r['verts']:
        ox, oy = v.co.x, v.co.y
        v.co.x = hx;  v.co.y = ox * hy;  v.co.z = oy * hz

    r = bmesh.ops.create_grid(bm, x_segments=ny, y_segments=nz, size=1.0)
    for v in r['verts']:
        ox, oy = v.co.x, v.co.y
        v.co.x = -hx;  v.co.y = -ox * hy;  v.co.z = oy * hz

    r = bmesh.ops.create_grid(bm, x_segments=nx, y_segments=nz, size=1.0)
    for v in r['verts']:
        ox, oy = v.co.x, v.co.y
        v.co.x = -ox * hx;  v.co.y = hy;  v.co.z = oy * hz

    r = bmesh.ops.create_grid(bm, x_segments=nx, y_segments=nz, size=1.0)
    for v in r['verts']:
        ox, oy = v.co.x, v.co.y
        v.co.x = ox * hx;  v.co.y = -hy;  v.co.z = oy * hz

    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bmesh.ops.translate(bm, vec=bbox_center, verts=bm.verts)

    return bm


class BAKER_OT_GenerateCage(Operator):
    bl_idname  = "baker.generate_cage"
    bl_label   = "Generate Cage Mesh"
    bl_description = (
        "Build a bbox cage with square quads and a live Shrinkwrap. "
        "Modifiers on source objects are applied automatically."
    )
    bl_options = {'REGISTER', 'UNDO'}

    def _make_evaluated_dupe(self, context, obj):
        depsgraph = context.evaluated_depsgraph_get()
        obj_eval  = obj.evaluated_get(depsgraph)
        mesh      = bpy.data.meshes.new_from_object(obj_eval)
        dupe      = bpy.data.objects.new(_TEMP_PREFIX, mesh)
        dupe.matrix_world = obj.matrix_world.copy()
        context.collection.objects.link(dupe)
        return dupe

    def execute(self, context):
        import mathutils

        s = context.scene.baker_settings

        view3d_area, view3d_region = _get_view3d_context(context)
        if view3d_area is None:
            self.report({'ERROR'}, "No 3D Viewport found.")
            return {'CANCELLED'}

        if s.multi_source:
            sources = [item.obj for item in s.high_poly_list if item.obj is not None]
            if not sources:
                self.report({'ERROR'}, "Add at least one object to the high-poly list first.")
                return {'CANCELLED'}
            cage_name = "MultiSource_Cage"
        else:
            if s.high_poly is None:
                self.report({'ERROR'}, "Please assign a High-Poly object first.")
                return {'CANCELLED'}
            sources   = [s.high_poly]
            cage_name = s.high_poly.name + "_Cage"

        _purge_temps(cage_name)

        temp = None
        try:
            dupes = [self._make_evaluated_dupe(context, obj) for obj in sources]

            bpy.ops.object.select_all(action='DESELECT')
            for d in dupes:
                d.select_set(True)
            context.view_layer.objects.active = dupes[0]
            if len(dupes) > 1:
                bpy.ops.object.join()
            temp      = context.active_object
            temp.name = _TEMP_PREFIX + cage_name

            mw      = temp.matrix_world
            corners = [mw @ mathutils.Vector(c) for c in temp.bound_box]
            xs = [v.x for v in corners]
            ys = [v.y for v in corners]
            zs = [v.z for v in corners]

            off     = s.cage_offset
            bb_min  = mathutils.Vector((min(xs) - off, min(ys) - off, min(zs) - off))
            bb_max  = mathutils.Vector((max(xs) + off, max(ys) + off, max(zs) + off))
            bb_size = bb_max - bb_min
            bb_ctr  = (bb_min + bb_max) / 2.0

            bm = _build_cage_bmesh(bb_ctr, bb_size, s.cage_poly_size)

            old = bpy.data.objects.get(cage_name)
            if old is not None:
                bpy.data.objects.remove(old, do_unlink=True)

            mesh = bpy.data.meshes.new(cage_name)
            bm.to_mesh(mesh)
            bm.free()
            mesh.update()

            cage_obj = bpy.data.objects.new(cage_name, mesh)
            context.collection.objects.link(cage_obj)
            cage_obj.rotation_euler = (s.cage_rotation_x, s.cage_rotation_y, s.cage_rotation_z)

            bpy.ops.object.select_all(action='DESELECT')
            cage_obj.select_set(True)
            context.view_layer.objects.active = cage_obj

            with context.temp_override(area=view3d_area, region=view3d_region):
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.uv.smart_project(
                    angle_limit=1.15192,
                    island_margin=0.02,
                    rotate_method='AXIS_ALIGNED_Y',
                )
                bpy.ops.object.mode_set(mode='OBJECT')

            sw             = cage_obj.modifiers.new(name="Shrinkwrap", type='SHRINKWRAP')
            sw.target      = temp
            sw.wrap_method = 'NEAREST_SURFACEPOINT'
            sw.wrap_mode   = 'ON_SURFACE'
            sw.offset      = 0.0

            s.low_poly = cage_obj

            src_desc = f"{len(sources)} objects" if s.multi_source else sources[0].name
            self.report({'INFO'},
                f"Cage '{cage_name}': {len(mesh.polygons)} quads shrink-wrapped to "
                f"{src_desc}. Rotate with sliders — ready to bake."
            )
            return {'FINISHED'}

        except Exception as e:
            self.report({'ERROR'}, f"Cage generation failed: {e}")
            return {'CANCELLED'}

        finally:
            if temp is not None:
                temp.hide_set(True)
                temp.hide_render = True


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

_SPACE  = 'VIEW_3D'
_REGION = 'UI'
_CAT    = "Grillen"


def _section_header(layout, text, icon='NONE'):
    row = layout.row(align=False)
    row.scale_y = 0.8
    row.label(text=text, icon=icon)


def _prop_split(layout):
    layout.use_property_split    = True
    layout.use_property_decorate = False


def _dirty_icon(is_clean):
    return 'CHECKMARK' if is_clean else 'PROP_OFF'


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

class BAKER_PT_Main(Panel):
    bl_label       = "Grillen v2.0.1"
    bl_idname      = "BAKER_PT_main"
    bl_space_type  = _SPACE
    bl_region_type = _REGION
    bl_category    = _CAT

    def draw_header(self, context):
        self.layout.label(text="", icon='RENDER_STILL')

    def draw(self, context):
        layout = self.layout
        s      = context.scene.baker_settings
        _prop_split(layout)

        # ── OBJECTS ──────────────────────────────────────────────
        _section_header(layout, "OBJECTS", 'OUTLINER_OB_MESH')
        box = layout.box()
        _prop_split(box)
        box.prop(s, "multi_source")
        box.separator(factor=0.4)

        col = box.column(align=True)
        if s.multi_source:
            row = col.row(align=False)
            row.label(text="High-Poly Sources:")
            list_row = col.row()
            list_row.template_list(
                "BAKER_UL_high_poly_list", "",
                s, "high_poly_list",
                s, "high_poly_list_index",
                rows=3,
            )
            btn_col = list_row.column(align=True)
            btn_col.operator("baker.high_poly_add",    text="", icon='ADD')
            btn_col.operator("baker.high_poly_remove", text="", icon='REMOVE')
            btn_col.separator()
            btn_col.operator("baker.high_poly_clear",  text="", icon='TRASH')
            hint = col.row(); hint.scale_y = 0.7
            hint.label(text="Add = all selected mesh objects", icon='INFO')
        else:
            row = col.row(align=True)
            row.alert = s.high_poly is None
            row.prop(s, "high_poly", icon='MESH_ICOSPHERE')

        col.separator(factor=0.4)
        row = col.row(align=True)
        row.alert = s.low_poly is None
        row.prop(s, "low_poly", icon='MESH_GRID')

        # Quick-add to bake queue
        box.separator(factor=0.4)
        q_row = box.row(align=True)
        q_row.enabled = (
            (s.high_poly is not None or len([i for i in s.high_poly_list if i.obj]) > 0)
            and s.low_poly is not None
        )
        q_row.operator("baker.queue_add", text="Add to Queue", icon='APPEND_BLEND')
        q_count = len(s.queue)
        if q_count > 0:
            q_row.label(text=f"({q_count})")

        layout.separator(factor=0.6)

        # ── MAPS ─────────────────────────────────────────────────
        _section_header(layout, "MAPS", 'NODE_MATERIAL')
        box = layout.box()
        _prop_split(box)
        box.prop(s, "resolution")
        box.separator(factor=0.3)

        MAPS = [
            ('bake_normals',   'Normal',  'NORMALS_FACE', s.normal_baked),
            ('bake_ao',        'AO',      'LIGHT_SUN',    s.ao_baked),
            ('bake_diffuse',   'Diffuse',   'IMAGE_RGB',    s.diffuse_baked),
            ('bake_roughness', 'Roughness','ANTIALIASED',  s.roughness_baked),
            ('bake_metalness', 'Metalness','MATFLUID',     s.metalness_baked),
            ('bake_alpha',     'Alpha',   'IMAGE_ALPHA',  s.alpha_baked),
        ]

        # Map buttons — constrained to 80% width via split, right side absorbs stretch
        split = box.split(factor=0.8)
        btn_col = split.column(align=True)
        btn_col.scale_y = 1.2
        for i in range(0, len(MAPS), 2):
            row = btn_col.row(align=True)
            for j in range(2):
                if i + j < len(MAPS):
                    prop, label, icon, is_clean = MAPS[i + j]
                    enabled = getattr(s, prop)
                    sub = row.row(align=True)
                    sub.alert = enabled and not is_clean
                    sub.prop(s, prop, text=label, icon=icon, toggle=True)
        split.column()  # empty absorber

        if s.bake_normals:
            box.separator(factor=0.4)
            sub = box.box()
            hdr = sub.row(align=True)
            hdr.scale_y = 0.85
            hdr.label(text="NORMAL MAP", icon='NORMALS_FACE')
            col3 = sub.column(align=True)
            _prop_split(col3)
            col3.prop(s, "normal_supersample", text="Supersample (2×)")

        if s.bake_alpha:
            box.separator(factor=0.4)
            sub = box.box()
            hdr = sub.row(align=True)
            hdr.scale_y = 0.85
            hdr.label(text="ALPHA MAP", icon='IMAGE_ALPHA')
            sub.prop(s, "alpha_mode", text="")
            col2 = sub.column(align=True)
            _prop_split(col2)
            col2.prop(s, "alpha_supersample", text="Supersample (2×)")
            col2.prop(s, "alpha_smooth_sigma")

        box.separator(factor=0.3)
        box.prop(s, "skip_clean_maps")

        layout.separator(factor=0.6)

        # ── BAKE ─────────────────────────────────────────────────
        if s.multi_source:
            ready = len([i for i in s.high_poly_list if i.obj]) > 0 and s.low_poly is not None
        else:
            ready = s.high_poly is not None and s.low_poly is not None

        col = layout.column(align=True)
        col.enabled = ready
        col.scale_y = 1.9
        col.operator("baker.bake", text="BAKE", icon='RENDER_STILL')


class BAKER_PT_Settings(Panel):
    bl_label       = "Settings"
    bl_idname      = "BAKER_PT_settings"
    bl_space_type  = _SPACE
    bl_region_type = _REGION
    bl_category    = _CAT
    bl_parent_id   = "BAKER_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='TOOL_SETTINGS')

    def draw(self, context):
        layout = self.layout
        s      = context.scene.baker_settings
        _prop_split(layout)

        _section_header(layout, "MARGIN", 'IMAGE_DATA')
        box = layout.box()
        _prop_split(box)
        col = box.column(align=True)
        col.prop(s, "margin")
        col.prop(s, "margin_type")

        layout.separator(factor=0.6)

        _section_header(layout, "RAY CASTING", 'FORCE_MAGNETIC')
        box = layout.box()
        _prop_split(box)
        box.prop(s, "auto_ray_cast")
        col = box.column(align=True)
        col.enabled = not s.auto_ray_cast
        col.prop(s, "cage_extrusion")
        col.prop(s, "max_ray_distance")

        layout.separator(factor=0.6)

        _section_header(layout, "OPTIONS", 'PROPERTIES')
        box = layout.box()
        _prop_split(box)
        col = box.column(align=True)
        col.prop(s, "overwrite_images")
        col.prop(s, "preserve_materials")
        col.prop(s, "use_generated_cage")
        col.prop(s, "auto_apply_scale")
        col.prop(s, "post_bake_preview")


class BAKER_PT_Output(Panel):
    bl_label       = "Output"
    bl_idname      = "BAKER_PT_output"
    bl_space_type  = _SPACE
    bl_region_type = _REGION
    bl_category    = _CAT
    bl_parent_id   = "BAKER_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='EXPORT')

    def draw(self, context):
        layout = self.layout
        s      = context.scene.baker_settings
        _prop_split(layout)

        # ── DESTINATION ──────────────────────────────────────────
        _section_header(layout, "DESTINATION", 'FILE_FOLDER')
        box = layout.box()
        _prop_split(box)
        col = box.column(align=True)
        col.prop(s, "output_dir", text="Folder")
        col.prop(s, "prefix",     text="Prefix")

        layout.separator(factor=0.6)

        # ── FORMAT ───────────────────────────────────────────────
        _section_header(layout, "FORMAT", 'IMAGE_DATA')
        box = layout.box()
        _prop_split(box)
        col = box.column(align=True)
        col.prop(s, "output_format")
        # Show only valid bit depths for the selected format
        row = col.row(align=True)
        if s.output_format == 'TARGA':
            row.enabled = False
        row.prop(s, "output_depth")

        layout.separator(factor=0.6)

        # ── NORMAL MAP ───────────────────────────────────────────
        _section_header(layout, "NORMAL MAP", 'NORMALS_FACE')
        box = layout.box()
        _prop_split(box)
        box.prop(s, "normal_convention")

        layout.separator(factor=0.6)

        # ── CHANNEL PACKING ──────────────────────────────────────
        _section_header(layout, "CHANNEL PACKING", 'NODE_COMPOSITING')
        box = layout.box()
        _prop_split(box)
        box.prop(s, "export_orm")
        if s.export_orm:
            hint = box.row()
            hint.scale_y = 0.65
            hint.label(text="R=AO  G=Roughness  B=Metallic", icon='INFO')


class BAKER_PT_Queue(Panel):
    bl_label       = "Bake Queue"
    bl_idname      = "BAKER_PT_queue"
    bl_space_type  = _SPACE
    bl_region_type = _REGION
    bl_category    = _CAT
    bl_parent_id   = "BAKER_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='SORTTIME')

    def draw(self, context):
        layout = self.layout
        s      = context.scene.baker_settings
        _prop_split(layout)

        _section_header(layout, "QUEUE", 'SHORTDISPLAY')
        box = layout.box()
        list_row = box.row()
        list_row.template_list(
            "BAKER_UL_queue_list", "",
            s, "queue",
            s, "queue_index",
            rows=3,
        )
        btn_col = list_row.column(align=True)
        btn_col.operator("baker.queue_add",    text="", icon='ADD')
        btn_col.operator("baker.queue_remove", text="", icon='REMOVE')
        btn_col.separator()
        btn_col.operator("baker.queue_clear",  text="", icon='TRASH')

        hint = box.row(); hint.scale_y = 0.7
        hint.label(text="Add = current High/Low pair", icon='INFO')

        layout.separator(factor=0.6)
        row = layout.row()
        row.enabled = len([i for i in s.queue if i.enabled]) > 0
        row.scale_y = 1.5
        row.operator("baker.bake_queue", text="Bake Queue", icon='RENDER_ANIMATION')


class BAKER_PT_NodeBake(Panel):
    bl_label       = "Node Bake"
    bl_idname      = "BAKER_PT_node_bake"
    bl_space_type  = _SPACE
    bl_region_type = _REGION
    bl_category    = _CAT
    bl_parent_id   = "BAKER_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='NODE_COMPOSITING')

    def draw(self, context):
        layout = self.layout
        _prop_split(layout)

        _section_header(layout, "BAKE SHADER NODE", 'EXPORT')
        box = layout.box()

        row = box.row()
        row.scale_y = 1.3
        row.operator("baker.bake_selected_node", text="Bake Active Node", icon='EXPORT')

        hint = box.row()
        hint.scale_y = 0.65
        hint.label(text="Select node in Shader Editor first", icon='INFO')


class BAKER_PT_Cage(Panel):
    bl_label       = "Cage Generator"
    bl_idname      = "BAKER_PT_cage"
    bl_space_type  = _SPACE
    bl_region_type = _REGION
    bl_category    = _CAT
    bl_parent_id   = "BAKER_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='MOD_SHRINKWRAP')

    def draw(self, context):
        layout = self.layout
        s      = context.scene.baker_settings
        _prop_split(layout)

        _section_header(layout, "MESH", 'SURFACE_DATA')
        box = layout.box()
        _prop_split(box)
        col = box.column(align=True)
        col.prop(s, "cage_poly_size")

        # Offset row with auto button
        row = col.row(align=True)
        row.prop(s, "cage_offset")
        row.operator("baker.auto_cage_offset", text="", icon='AUTO')

        # Live face count estimate
        if s.multi_source:
            sources = [i.obj for i in s.high_poly_list if i.obj]
        else:
            sources = [s.high_poly] if s.high_poly else []

        if sources:
            est = _estimate_cage_faces(sources, s.cage_poly_size, s.cage_offset)
            if est is not None:
                info = box.row(); info.scale_y = 0.75
                color = 'ERROR' if est > 500_000 else 'INFO'
                info.label(
                    text=f"Est. {est:,} faces",
                    icon='ERROR' if est > 500_000 else 'CHECKMARK'
                )

        layout.separator(factor=0.6)

        _section_header(layout, "ROTATION", 'ORIENTATION_GIMBAL')
        box = layout.box()
        _prop_split(box)
        col = box.column(align=True)
        col.prop(s, "cage_rotation_x", text="X")
        col.prop(s, "cage_rotation_y", text="Y")
        col.prop(s, "cage_rotation_z", text="Z")

        layout.separator(factor=0.6)

        if s.multi_source:
            ready = len(sources) > 0
            label = f"Generate Cage  ({len(sources)} sources)"
        else:
            ready = s.high_poly is not None
            label = "Generate Cage"

        row = layout.row()
        row.enabled = ready
        row.scale_y = 1.4
        row.operator("baker.generate_cage", text=label, icon='MOD_SHRINKWRAP')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    BAKER_PG_HighPolyItem,
    BAKER_PG_QueueItem,
    BAKER_PG_Settings,
    BAKER_UL_HighPolyList,
    BAKER_UL_QueueList,
    BAKER_OT_HighPolyAdd,
    BAKER_OT_HighPolyRemove,
    BAKER_OT_HighPolyClear,
    BAKER_OT_QueueAdd,
    BAKER_OT_QueueRemove,
    BAKER_OT_QueueClear,
    BAKER_OT_BakeQueue,
    BAKER_OT_Bake,
    BAKER_OT_BakeSelectedNode,
    BAKER_OT_AutoCageOffset,
    BAKER_OT_GenerateCage,
    BAKER_PT_Main,
    BAKER_PT_Settings,
    BAKER_PT_Output,
    BAKER_PT_Queue,
    BAKER_PT_NodeBake,
    BAKER_PT_Cage,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.baker_settings = PointerProperty(type=BAKER_PG_Settings)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.baker_settings


if __name__ == "__main__":
    register()
