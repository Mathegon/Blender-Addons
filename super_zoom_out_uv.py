bl_info = {
    "name": "Super Zoom Out (UV Editor)",
    "author": "Mattias + Copilot",
    "version": (1, 0),
    "blender": (3, 0, 0),
    "location": "UV Editor > Sidebar > View Tab",
    "description": "Overrides Blender's UV Editor zoom limit with a single button.",
    "category": "UV",
}

import bpy

def force_uv_zoom_out(amount=0.0001):
    """Force the UV editor zoom to a tiny value."""
    for area in bpy.context.screen.areas:
        if area.type == 'IMAGE_EDITOR':
            for space in area.spaces:
                if space.type == 'IMAGE_EDITOR':
                    space.zoom = (amount, amount)

class UV_OT_SuperZoomOut(bpy.types.Operator):
    """Force UV Editor to zoom out beyond normal limits"""
    bl_idname = "uv.super_zoom_out"
    bl_label = "Super Zoom Out"
    bl_options = {'REGISTER', 'UNDO'}

    zoom_amount: bpy.props.FloatProperty(
        name="Zoom Amount",
        description="Smaller = more zoomed out",
        default=0.0001,
        min=0.0000001,
        max=1.0
    )

    def execute(self, context):
        force_uv_zoom_out(self.zoom_amount)
        self.report({'INFO'}, f"UV zoom set to {self.zoom_amount}")
        return {'FINISHED'}

class UV_PT_SuperZoomPanel(bpy.types.Panel):
    """Creates a panel in the UV Editor sidebar"""
    bl_label = "Super Zoom Out"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "View"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Override UV Zoom Limit")
        layout.operator("uv.super_zoom_out")

def register():
    bpy.utils.register_class(UV_OT_SuperZoomOut)
    bpy.utils.register_class(UV_PT_SuperZoomPanel)

def unregister():
    bpy.utils.unregister_class(UV_OT_SuperZoomOut)
    bpy.utils.unregister_class(UV_PT_SuperZoomPanel)

if __name__ == "__main__":
    register()
