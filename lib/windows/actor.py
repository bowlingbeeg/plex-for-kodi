# coding=utf-8
"""
Actor Detail Window - Shows actor biography, photo, and filmography
"""
from __future__ import absolute_import

import datetime

from kodi_six import xbmc
from kodi_six import xbmcgui

from lib import backgroundthread
from lib import util
from lib.util import T
from plexnet import util as plexnetUtil
from . import busy
from . import dropdown
from . import kodigui
from . import opener
from . import search
from . import windowutils

# Pagination settings
FILMOGRAPHY_PAGE_SIZE = 10

# Discover hub settings (Not in Library hubs - one per credit type)
DISCOVER_HUB_SLOTS = 6  # Max number of discover hub rows
NOT_IN_LIBRARY_BATCH_SIZE = 10  # GUIDs per library-check request (matches Plex Web)



class ActorDetailsTask(backgroundthread.Task):
    """Background task to fetch actor details from the server"""
    def __init__(self, role, callback):
        super(ActorDetailsTask, self).__init__()
        self.role = role
        self.callback = callback

    def run(self):
        if self.isCanceled():
            return

        details = self.role.getDetails()

        if not self.isCanceled():
            self.callback(details)


class ActorFilmographyTask(backgroundthread.Task):
    """Background task to fetch actor's filmography with pagination"""
    def __init__(self, role, media_type, callback, start=0, size=FILMOGRAPHY_PAGE_SIZE):
        super(ActorFilmographyTask, self).__init__()
        self.role = role
        self.media_type = media_type
        self.callback = callback
        self.start = start
        self.size = size

    def run(self):
        if self.isCanceled():
            return

        result = self.role.getFilmography(self.media_type, start=self.start, size=self.size)

        if not self.isCanceled():
            self.callback(result)


class ExtendFilmographyTask(backgroundthread.Task):
    """Background task to fetch more filmography items"""
    def setup(self, role, start, size, callback, canceledCallback=None):
        self.role = role
        self.start = start
        self.size = size
        self.callback = callback
        self.canceledCallback = canceledCallback
        return self

    def run(self):
        if self.isCanceled():
            if self.canceledCallback:
                self.canceledCallback()
            return

        try:
            result = self.role.getFilmography(None, start=self.start, size=self.size)
            if self.isCanceled():
                if self.canceledCallback:
                    self.canceledCallback()
                return
            self.callback(result)
        except Exception as e:
            util.DEBUG_LOG('ExtendFilmographyTask failed: {0}'.format(e))
            if self.canceledCallback:
                self.canceledCallback()


class DiscoverItem(object):
    """Lightweight wrapper around discover API credit metadata.
    Provides just enough attributes to create ManagedListItems and add to watchlist.
    """
    def __init__(self, credit_data):
        meta = credit_data.get('Metadata', {})
        self.title = meta.get('title', '')
        self.year = str(meta.get('year', ''))
        self.type = meta.get('type', 'movie')
        self.ratingKey = meta.get('ratingKey', '')
        self.guid = 'plex://{0}/{1}'.format(self.type, self.ratingKey)
        self.thumb = meta.get('thumb', '')
        self.art = meta.get('art', '')
        self.role = credit_data.get('role', '')
        self.order = credit_data.get('order', 999)
        self.is_discover = True  # Flag to distinguish from local PlexObjects


class DiscoverCreditsTask(backgroundthread.Task):
    """Background task to fetch full filmography from Plex discover API,
    then batch-check which items are in the user's library.
    Returns all credit groups (actor, director, writer, etc.) with library presence info."""

    def __init__(self, role, server, callback):
        super(DiscoverCreditsTask, self).__init__()
        self.role = role
        self.server = server
        self.callback = callback

    def run(self):
        if self.isCanceled():
            return

        # Step 1: Fetch ALL credit groups from discover (actor, director, writer, etc.)
        credit_groups = self.role.getDiscoverCredits(credit_type=None)
        if self.isCanceled() or not credit_groups:
            self.callback([], set(), set())
            return

        # Step 2: Build DiscoverItems for ALL groups, collect all GUIDs
        discover_hubs = []  # [(type_name, [DiscoverItem, ...]), ...]
        all_guids = []
        actor_guids = set()

        for group_type, credits in credit_groups:
            group_items = []
            for credit in credits:
                item = DiscoverItem(credit)
                if item.ratingKey:
                    group_items.append(item)
                    all_guids.append(item.guid)
                    if group_type.lower() == 'actor':
                        actor_guids.add(item.guid)
            if group_items:
                discover_hubs.append((group_type, group_items))

        if self.isCanceled():
            self.callback([], set(), set())
            return

        # Step 3: Batch-check which GUIDs are in the local library (deduplicated)
        unique_guids = list(set(all_guids))
        from plexnet import media as plexmedia
        library_guids = plexmedia.Role.checkLibraryPresence(self.server, unique_guids)

        if not self.isCanceled():
            self.callback(discover_hubs, library_guids, actor_guids)


class ActorWindow(kodigui.ControlledWindow, windowutils.UtilMixin):
    xmlFile = 'script-plex-actor.xml'
    path = util.ADDON.getAddonInfo('path')
    theme = 'Main'
    res = '1080i'
    width = 1920
    height = 1080

    THUMB_DIM = util.scaleResolution(300, 300)
    POSTER_DIM = util.scaleResolution(244, 361)

    FILMOGRAPHY_LIST_ID = 400
    DISCOVER_LIST_BASE_ID = 401  # List IDs 401-406 for discover hubs
    DISCOVER_GROUP_BASE_ID = 501  # Group IDs 501-506 for discover hubs
    HOME_BUTTON_ID = 201
    SEARCH_BUTTON_ID = 202
    PLAYER_STATUS_BUTTON_ID = 204

    def __init__(self, *args, **kwargs):
        kodigui.ControlledWindow.__init__(self, *args, **kwargs)
        self.role = kwargs.get('role')
        self.actorDetails = None
        self.filmographyItems = []  # Unique items (one per GUID)
        self.filmographyAllItems = []  # All raw items from API
        self.filmographyByGuid = {}  # {guid: [item1, item2, ...]} for multi-library handling
        self.filmographyOffset = 0
        self.filmographyTotalSize = 0
        self.filmographyMore = False
        self.discoverListControls = []  # ManagedControlList for each discover hub slot
        self.discoverActorGuids = set()  # GUIDs from discover actor credits (for filtering)
        self.libraryGuids = set()  # GUIDs confirmed in user's library
        self.tasks = backgroundthread.Tasks()
        self.exitCommand = None
        self.initialized = False

    def onFirstInit(self):
        self.filmographyListControl = kodigui.ManagedControlList(self, self.FILMOGRAPHY_LIST_ID, 5)

        # Create list controls for all discover hub slots (template generates 6)
        self.discoverListControls = []
        for i in range(DISCOVER_HUB_SLOTS):
            list_id = self.DISCOVER_LIST_BASE_ID + i
            try:
                control = kodigui.ManagedControlList(self, list_id, 5)
                self.discoverListControls.append(control)
            except Exception:
                break

        # Set initial info from role object
        self.setProperty('actor.name', self.role.tag or '')
        if self.role.thumb:
            self.setProperty('actor.thumb', self.role.thumb.asTranscodedImageURL(*self.THUMB_DIM))

        # Fetch full details in background
        self.fetchActorDetails()
        self.fetchFilmography()
        self.fetchDiscoverCredits()

        self.initialized = True

    def onReInit(self):
        pass

    def onAction(self, action):
        try:
            controlID = self.getFocusId()
            if action in (xbmcgui.ACTION_NAV_BACK, xbmcgui.ACTION_PREVIOUS_MENU):
                self.doClose()
                return

            # Handle filmography pagination when user scrolls to end marker
            if controlID == self.FILMOGRAPHY_LIST_ID:
                if self.checkFilmographyPagination(action):
                    return

        except Exception:
            util.ERROR()

        kodigui.ControlledWindow.onAction(self, action)

    def checkFilmographyPagination(self, action):
        """Check if we need to load more filmography items"""
        mli = self.filmographyListControl.getSelectedItem()
        if not mli:
            return False

        # Check if we're on the "load more" marker
        if mli.getProperty('is.end') and not mli.getProperty('is.updating'):
            # User scrolled to the end marker, load more items
            mli.setBoolProperty('is.updating', True)
            self.extendFilmography()
            return True

        return False

    def onClick(self, controlID):
        if controlID == self.HOME_BUTTON_ID:
            self.goHome()
        elif controlID == self.FILMOGRAPHY_LIST_ID:
            self.filmographyItemClicked()
        elif controlID == self.SEARCH_BUTTON_ID:
            self.searchButtonClicked()
        elif controlID == self.PLAYER_STATUS_BUTTON_ID:
            self.showAudioPlayer()
        elif self.DISCOVER_LIST_BASE_ID <= controlID < self.DISCOVER_LIST_BASE_ID + DISCOVER_HUB_SLOTS:
            self.openDiscoverItem(controlID)

    def onFocus(self, controlID):
        if self.FILMOGRAPHY_LIST_ID <= controlID <= self.DISCOVER_LIST_BASE_ID + DISCOVER_HUB_SLOTS:
            self.setProperty('hub.focus', str(controlID - self.FILMOGRAPHY_LIST_ID))

    def doClose(self, **kw):
        self.tasks.kill()
        kodigui.ControlledWindow.doClose(self)

    def fetchActorDetails(self):
        task = ActorDetailsTask(self.role, self.onActorDetails)
        self.tasks.add(task)
        backgroundthread.BGThreader.addTask(task)

    def fetchFilmography(self):
        self.setProperty('loading', '1')
        task = ActorFilmographyTask(self.role, None, self.onFilmography, start=0, size=FILMOGRAPHY_PAGE_SIZE)
        self.tasks.add(task)
        backgroundthread.BGThreader.addTask(task)

    def extendFilmography(self):
        """Fetch more filmography items"""
        start = self.filmographyOffset + len(self.filmographyItems)
        task = ExtendFilmographyTask().setup(
            self.role,
            start=start,
            size=FILMOGRAPHY_PAGE_SIZE,
            callback=self.onFilmographyExtended,
            canceledCallback=self.onFilmographyExtendCanceled
        )
        self.tasks.add(task)
        backgroundthread.BGThreader.addTask(task)

    def onFilmographyExtendCanceled(self):
        """Handle extension task cancellation"""
        # Find and clear the is.updating property on the end marker
        for mli in self.filmographyListControl:
            if mli.getProperty('is.end'):
                mli.setBoolProperty('is.updating', False)
                break

    def onFilmographyExtended(self, result):
        """Handle additional filmography items"""
        items = result.get('items', [])
        self.filmographyMore = result.get('more', False)
        self.filmographyTotalSize = result.get('totalSize', 0)

        if not items:
            # No more items, remove the end marker
            self.onFilmographyExtendCanceled()
            return

        # Add new items to all items list
        self.filmographyAllItems.extend(items)
        
        # Group new items by GUID and merge with existing
        newUniqueItems, newByGuid = self.groupFilmographyByGuid(items, existingByGuid=self.filmographyByGuid)
        self.filmographyItems.extend(newUniqueItems)

        # Create list items for the new unique items only
        newListItems = []
        for item in newUniqueItems:
            mli = self.createFilmographyListItem(item)
            newListItems.append(mli)

        # Add end marker if there are more items
        if self.filmographyMore:
            end = kodigui.ManagedListItem('')
            end.setBoolProperty('is.end', True)
            newListItems.append(end)

        # Replace the old end marker with new items
        endPos = self.filmographyListControl.size() - 1
        self.filmographyListControl.replaceItem(endPos, newListItems[0])
        if len(newListItems) > 1:
            self.filmographyListControl.addItems(newListItems[1:])

        # Select the first new item
        self.filmographyListControl.selectItem(endPos)

        # Update count
        self.setProperty('filmography.count', str(len(self.filmographyItems)))

    def onActorDetails(self, details):
        if not details:
            util.DEBUG_LOG('ActorWindow: No details returned for actor')
            return

        util.DEBUG_LOG('ActorWindow: Got actor details - name={}, summary_len={}, birthDate={}'.format(
            details.get('name', ''),
            len(details.get('summary', '')),
            details.get('birthDate', '')
        ))

        self.actorDetails = details
        self.setProperty('actor.name', details.get('name', ''))
        self.setProperty('actor.summary', details.get('summary', ''))
        self.setProperty('actor.birthPlace', details.get('birthPlace', ''))

        # Handle birth date and age calculation
        birthDate = details.get('birthDate', '')
        deathDate = details.get('deathDate', '')

        if birthDate:
            self.setProperty('actor.birthDate', self.formatDate(birthDate))
            age = self.calculateAge(birthDate, deathDate)
            if age:
                self.setProperty('actor.age', str(age))

        if deathDate:
            self.setProperty('actor.deathDate', self.formatDate(deathDate))
            self.setProperty('actor.deceased', '1')

        # Update thumb if we got a better one
        thumb = details.get('thumb', '')
        if thumb:
            self.setProperty('actor.thumb', self.role.server.getImageTranscodeURL(thumb, *self.THUMB_DIM))

    def fetchDiscoverCredits(self):
        """Fetch full filmography from discover API and check library presence"""
        if not hasattr(self.role, 'tagKey') or not self.role.tagKey:
            util.DEBUG_LOG('ActorWindow: No tagKey, skipping discover credits')
            return

        task = DiscoverCreditsTask(self.role, self.role.server, self.onDiscoverCredits)
        self.tasks.add(task)
        backgroundthread.BGThreader.addTask(task)

    def onDiscoverCredits(self, discover_hubs, library_guids, actor_guids):
        """Handle discover credits results — populate one hub per credit type with not-in-library items"""
        self.libraryGuids = library_guids
        self.discoverActorGuids = actor_guids

        # Filter each group to not-in-library items and populate sequential hub slots
        slot = 0
        for group_type, items in discover_hubs:
            if slot >= DISCOVER_HUB_SLOTS:
                break
            not_in_library = [item for item in items if item.guid not in library_guids]
            if not_in_library:
                label = '{0} - {1}'.format(T(32479, 'Not in Library'), group_type.title())
                self.fillDiscoverHub(slot, not_in_library, label)
                slot += 1

        util.DEBUG_LOG('ActorWindow: Discover credits: {0} groups, {1} in library, {2} hubs populated'.format(
            len(discover_hubs), len(library_guids), slot))

        # Now filter the existing filmography to actor-only credits
        self.filterFilmographyToActorCredits()

    def filterFilmographyToActorCredits(self):
        """Remove non-acting credits from the filmography list using discover data"""
        if not self.discoverActorGuids or not self.filmographyItems:
            return

        original_count = len(self.filmographyItems)
        filtered = []
        for item in self.filmographyItems:
            guid = self.getItemGuid(item)
            # Keep item if its GUID is in the actor credits, or if we can't check (no GUID)
            if not guid or guid in self.discoverActorGuids:
                filtered.append(item)

        if len(filtered) < original_count:
            util.DEBUG_LOG('ActorWindow: Filtered filmography from {0} to {1} (actor credits only)'.format(
                original_count, len(filtered)))
            self.filmographyItems = filtered
            self.fillFilmography()

    def fillDiscoverHub(self, slot, items, label):
        """Populate a discover hub slot with items and set its label"""
        if slot >= len(self.discoverListControls):
            return

        listControl = self.discoverListControls[slot]
        listItems = []
        for item in items:
            mli = self.createNotInLibraryListItem(item)
            listItems.append(mli)

        listControl.reset()
        listControl.addItems(listItems)
        self.setProperty('discover.hub.{0}.label'.format(slot), label)

    def createNotInLibraryListItem(self, item):
        """Create a ManagedListItem from a DiscoverItem"""
        mli = kodigui.ManagedListItem(
            item.title,
            item.year,
            thumbnailImage=item.thumb,
            data_source=item
        )
        mli.setProperty('media.type', item.type)
        mli.setProperty('thumb.fallback', 'script.plex/thumb_fallbacks/{0}.png'.format(
            'show' if item.type == 'show' else 'movie'))
        if item.role:
            mli.setProperty('role', item.role)
        return mli

    def openDiscoverItem(self, controlID):
        """Open a discover item in the watchlist preplay screen"""
        slot = controlID - self.DISCOVER_LIST_BASE_ID
        if slot < 0 or slot >= len(self.discoverListControls):
            return

        mli = self.discoverListControls[slot].getSelectedItem()
        if not mli or not mli.dataSource:
            return

        item = mli.dataSource
        if not item.ratingKey:
            return

        from plexnet import util as pnUtil
        discover_server = pnUtil.SERVERMANAGER.getDiscoverServer()
        if not discover_server:
            util.DEBUG_LOG('ActorWindow: No discover server available')
            return

        # Pass ratingKey as string — opener.open() fetches the full object from the
        # discover server, then routes to PrePlayWindowWL (movies) or ShowWindow (shows)
        self.processCommand(opener.open(
            item.ratingKey,
            server=discover_server,
            from_watchlist=True,
            external_item=True
        ))

    def onFilmography(self, result):
        self.setProperty('loading', '')

        # Handle the new result format with pagination info
        items = result.get('items', [])
        self.filmographyAllItems = items
        self.filmographyOffset = result.get('offset', 0)
        self.filmographyTotalSize = result.get('totalSize', len(items))
        self.filmographyMore = result.get('more', False)

        # Group items by GUID to handle multi-library duplicates
        self.filmographyItems, self.filmographyByGuid = self.groupFilmographyByGuid(items)

        self.fillFilmography()

    def createFilmographyListItem(self, item):
        """Create a ManagedListItem for a filmography item"""
        title = item.title if hasattr(item, 'title') else item.get('title', '')
        year = ''
        if hasattr(item, 'year'):
            year = str(item.year) if item.year else ''

        thumb = ''
        if hasattr(item, 'thumb') and item.thumb:
            thumb = item.thumb.asTranscodedImageURL(*self.POSTER_DIM)
        elif hasattr(item, 'defaultThumb') and item.defaultThumb:
            thumb = item.defaultThumb.asTranscodedImageURL(*self.POSTER_DIM)

        mli = kodigui.ManagedListItem(title, year, thumbnailImage=thumb, data_source=item)

        # Set type indicator
        item_type = item.type if hasattr(item, 'type') else item.TYPE if hasattr(item, 'TYPE') else ''
        mli.setProperty('media.type', item_type)

        # Set watched indicator
        if hasattr(item, 'isWatched') and item.isWatched:
            mli.setProperty('watched', '1')

        # Thumb fallback
        mli.setProperty('thumb.fallback', 'script.plex/thumb_fallbacks/{0}.png'.format(
            item_type in ('show', 'season', 'episode') and 'show' or 'movie'))

        return mli

    def fillFilmography(self):
        """Populate the filmography list with initial items."""
        listItems = []

        for item in self.filmographyItems:
            mli = self.createFilmographyListItem(item)
            listItems.append(mli)

        # Add "load more" end marker if there are more items
        if self.filmographyMore:
            end = kodigui.ManagedListItem('')
            end.setBoolProperty('is.end', True)
            listItems.append(end)

        self.filmographyListControl.reset()
        self.filmographyListControl.addItems(listItems)

        # Update count
        self.setProperty('filmography.count', str(len(self.filmographyItems)))

    def filmographyItemClicked(self):
        mli = self.filmographyListControl.getSelectedItem()
        if not mli or not mli.dataSource:
            return

        item = mli.dataSource
        guid = self.getItemGuid(item)
        
        # Check if multiple versions exist
        versions = self.filmographyByGuid.get(guid, [item]) if guid else [item]
        
        if len(versions) > 1:
            # Show dropdown to choose version
            selectedItem = self.showVersionPicker(versions, item.type if hasattr(item, 'type') else 'movie')
            if selectedItem:
                self.processCommand(opener.open(selectedItem))
        else:
            # Single version, open directly
            self.processCommand(opener.open(item))

    def searchButtonClicked(self):
        self.processCommand(search.dialog(self))

    def formatDate(self, dateStr):
        """Format a date string (YYYY-MM-DD) to a display format"""
        if not dateStr:
            return ''

        try:
            # Parse YYYY-MM-DD format
            parts = dateStr.split('-')
            if len(parts) == 3:
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                dt = datetime.date(year, month, day)
                # Format as "Month Day, Year"
                return dt.strftime('%B %d, %Y')
        except (ValueError, IndexError):
            pass

        return dateStr

    def calculateAge(self, birthDateStr, deathDateStr=None):
        """Calculate age from birth date, optionally to death date"""
        if not birthDateStr:
            return None

        try:
            parts = birthDateStr.split('-')
            if len(parts) != 3:
                return None

            birthYear, birthMonth, birthDay = int(parts[0]), int(parts[1]), int(parts[2])
            birthDate = datetime.date(birthYear, birthMonth, birthDay)

            if deathDateStr:
                parts = deathDateStr.split('-')
                if len(parts) == 3:
                    endYear, endMonth, endDay = int(parts[0]), int(parts[1]), int(parts[2])
                    endDate = datetime.date(endYear, endMonth, endDay)
                else:
                    endDate = datetime.date.today()
            else:
                endDate = datetime.date.today()

            age = endDate.year - birthDate.year
            # Adjust if birthday hasn't occurred yet this year
            if (endDate.month, endDate.day) < (birthDate.month, birthDate.day):
                age -= 1

            return age
        except (ValueError, IndexError):
            return None

    def getItemGuid(self, item):
        """Get the GUID from a filmography item"""
        if hasattr(item, 'guid') and item.guid:
            return str(item.guid)
        return None

    def groupFilmographyByGuid(self, items, existingByGuid=None):
        """
        Group filmography items by GUID to handle multi-library duplicates.
        Returns (uniqueItems, byGuidDict) where uniqueItems has one item per GUID
        (the highest quality version) and byGuidDict maps GUID to all versions.
        """
        byGuid = existingByGuid if existingByGuid is not None else {}
        uniqueItems = []
        seenGuids = set(byGuid.keys()) if existingByGuid else set()
        
        for item in items:
            guid = self.getItemGuid(item)
            
            if guid:
                if guid not in byGuid:
                    byGuid[guid] = []
                byGuid[guid].append(item)
                
                # Only add to unique items if we haven't seen this GUID before
                if guid not in seenGuids:
                    seenGuids.add(guid)
                    uniqueItems.append(item)
            else:
                # No GUID, add as unique item
                uniqueItems.append(item)
        
        # Sort versions within each GUID by bitrate (highest first) for movies
        for guid, versions in byGuid.items():
            if len(versions) > 1:
                versions.sort(key=lambda v: self.getItemBitrate(v), reverse=True)
                # Replace the unique item with the highest quality version
                for i, uitem in enumerate(uniqueItems):
                    if self.getItemGuid(uitem) == guid:
                        uniqueItems[i] = versions[0]
                        break
        
        return uniqueItems, byGuid

    def getItemBitrate(self, item):
        """Get the bitrate from an item's media info"""
        try:
            if hasattr(item, 'media') and item.media:
                for media in item.media:
                    if hasattr(media, 'bitrate'):
                        return int(media.bitrate) if media.bitrate else 0
        except (ValueError, TypeError, AttributeError):
            pass
        return 0

    def getItemResolution(self, item):
        """Get the video resolution from an item's media info"""
        try:
            if hasattr(item, 'media') and item.media:
                for media in item.media:
                    if hasattr(media, 'videoResolution') and media.videoResolution:
                        return str(media.videoResolution)
        except (AttributeError, TypeError):
            pass
        return ''

    def getItemLibraryTitle(self, item):
        """Get the library section title for an item"""
        if hasattr(item, 'getLibrarySectionTitle'):
            return item.getLibrarySectionTitle()
        elif hasattr(item, 'librarySectionTitle'):
            return str(item.librarySectionTitle)
        return ''

    def formatVersionLabel(self, item, media_type='movie'):
        """Format a version label like watchlist: 'Library, Resolution (Bitrate)'"""
        library = self.getItemLibraryTitle(item) or T(34090, 'Unknown')
        
        if media_type == 'movie':
            resolution = self.getItemResolution(item)
            bitrate = self.getItemBitrate(item)
            
            if resolution:
                res_str = '{}p'.format(resolution) if 'k' not in str(resolution).lower() else resolution.upper()
            else:
                res_str = T(34090, 'Unknown')
            
            if bitrate:
                bitrate_str = plexnetUtil.bitrateToString(bitrate * 1000)
                return '{}, {} ({})'.format(library, res_str, bitrate_str)
            else:
                return '{}, {}'.format(library, res_str)
        else:
            # For shows, just show library name
            return library

    def showVersionPicker(self, versions, media_type='movie'):
        """Show a dropdown to pick which version to open"""
        options = []
        
        for idx, item in enumerate(versions):
            label = self.formatVersionLabel(item, media_type)
            options.append({
                'key': idx,
                'display': label
            })
        
        choice = dropdown.showDropdown(
            options=options,
            pos=(660, 441),
            close_direction='none',
            set_dropdown_prop=False,
            header=T(34091, 'Choose Version'),
            align_items='left'
        )
        
        if choice is not None:
            return versions[choice['key']]
        return None
