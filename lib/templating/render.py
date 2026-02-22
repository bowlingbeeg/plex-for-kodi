# coding=utf-8
import json
import os
import time
import copy

from .core import engine
# noinspection PyUnresolvedReferences
from lib.util import (DEF_THEME, ADDON, PROFILE, getSetting, translatePath, THEME_VERSION, setSetting, DEBUG_LOG, LOG,
                      T, MONITOR, xbmcvfs, addonSettings, DISPLAY_RESOLUTION, NEEDS_SCALING)
from .context import TEMPLATE_CONTEXTS
from .util import deep_update
from lib.os_utils import fast_glob
from lib.windows.busy import ProgressDialog


STEP_MAP = {
    "custom_templates": 33063,
    "default": 33064,
    "complete": 33065
}


def render_templates(theme=None, templates=None, force=False):
    # apply theme if version changed
    theme = theme or getSetting('theme', DEF_THEME)
    target_dir = os.path.join(translatePath(ADDON.getAddonInfo('path')), "resources", "skins", "Main", "1080i")

    # try to find custom theme_overrides.json in userdata
    custom_context_data_fn = os.path.join(PROFILE, "context_overrides.json")
    context = copy.deepcopy(TEMPLATE_CONTEXTS)
    if xbmcvfs.exists(custom_context_data_fn):
        try:
            f = xbmcvfs.File(custom_context_data_fn)
            data = f.read()
            f.close()
            if data:
                js = json.loads(data)
                deep_update(context, js)
                LOG("Loaded context overrides definitions from: {}".format(custom_context_data_fn))
        except:
            LOG("Couldn't load {}", custom_context_data_fn)

    if not engine.initialized:
        engine.init(target_dir, os.path.join(target_dir, "templates"),
                    os.path.join(translatePath(PROFILE), "templates"))

    engine.context = context
    engine.debug_log = DEBUG_LOG

    def apply():
        LOG("Rendering templates")
        start = time.time()

        with ProgressDialog(T(33062, ''), "", raise_hard=True) as pd:
            def update_progress(at, length, message):
                pd.update(int(at * 100 / float(length)),
                          message=T(STEP_MAP.get(message, STEP_MAP["default"]), '').format(message))

            # get template overrides
            watch_state_type = getSetting('watched_indicators', 'modern_2024')
            hub_count = getSetting('hub_count', 8)

            # Pre-calculate hub configurations for template (ibis doesn't support arithmetic)
            hubs = []
            for i in range(hub_count):
                hub_id = 400 + i
                hubs.append({
                    'index': i,
                    'hub_id': hub_id,
                    'group_id': 500 + i,
                    'spacer_id': 600 + i,
                    'is_first': i == 0,
                    'is_last': i == hub_count - 1,
                    'prev_hub_id': 101 if i == 0 else hub_id - 1,
                    'next_hub_id': hub_id if i == hub_count - 1 else hub_id + 1,
                })

            # Pre-calculate animation indexes for focus animations (starts at 1)
            animation_indexes = []
            for i in range(1, hub_count):
                animation_indexes.append({
                    'index': i,
                    'prev_group_id': 500 + i - 1,
                })

            # Pre-calculate grouplist height to fit all hubs
            # Content: section bar (200) + hubs (hub_count × 535) + dummy (100) + gaps ((hub_count+1) × 20)
            grouplist_height = 320 + 555 * hub_count

            overrides = {
                "core": {
                    "resolution": DISPLAY_RESOLUTION,
                    "needs_scaling": NEEDS_SCALING,
                    "hub_count": hub_count,
                    "hubs": hubs,
                    "animation_indexes": animation_indexes,
                    "grouplist_height": grouplist_height,
                },
                "indicators": {
                    "START": {
                        "INHERIT": watch_state_type,
                        "style": watch_state_type,
                        "hide_aw_bg": getSetting('hide_aw_bg', False),
                        "use_scaling": getSetting('scale_indicators', True)
                    }
                },
            }
            deep_update(context, overrides)

            engine.apply(theme, update_progress, templates=templates)
            end = time.time()
            MONITOR.waitForAbort(0.1)

        LOG("Rendered templates in: {:.2f}s".format(end - start))

    curThemeVer = getSetting('theme_version', 0)
    lastRes = getSetting('last_resolution', "1920x1080").split("x")
    lastSeenRes = [int(lastRes[0]), int(lastRes[1])]

    if curThemeVer != THEME_VERSION or (force or
                                       lastSeenRes != DISPLAY_RESOLUTION or
                                       addonSettings.alwaysCompileTemplates or
                                       len(fast_glob(os.path.join(engine.template_dir, "script-plex-*.xml.tpl"))) !=
                                       len(fast_glob(os.path.join(engine.target_dir, "script-plex-*.xml")))):
        setSetting('theme_version', THEME_VERSION)
        setSetting('last_resolution', "x".join(list(map(str, DISPLAY_RESOLUTION))))
        # apply seekdialog button theme
        apply()

    # lose template cache for performance reasons
    if not addonSettings.cacheTemplates:
        engine.loader.cache = {}
