# coding=utf-8
"""
Local mode ("Go local"): run against a local PMS only, without any plex.tv access.

Handles the account-less bootstrap (manual server entry, user seeding from the PMS'
/accounts endpoint) and storage of manually added local servers.
"""
from __future__ import absolute_import

import json

import requests

from xml.etree import ElementTree

from kodi_six import xbmcgui

from . import util
from .i18n import T


PROBE_TIMEOUT = 10


def getStoredServers():
    try:
        servers = json.loads(util.getSetting('local_servers_json', '') or '[]')
    except ValueError:
        servers = []
    return [s for s in servers if isinstance(s, dict) and s.get('connection')]


def saveStoredServers(servers):
    util.setSetting('local_servers_json', json.dumps(servers))


def probe(ip, port, token=None):
    """
    Check whether a PMS answers at ip:port. Returns (ok, name, needsAuth).
    /identity answers unauthenticated; the root endpoint tells us whether the
    given token (or no token) is enough for actual library access.
    """
    base = 'http://{0}:{1}'.format(ip, port)
    try:
        r = requests.get(base + '/identity', timeout=PROBE_TIMEOUT)
        if r.status_code != 200:
            return False, None, False
    except Exception:
        return False, None, False

    name = None
    needsAuth = False
    try:
        headers = {'Accept': 'application/xml'}
        if token:
            headers['X-Plex-Token'] = token
        r = requests.get(base + '/', headers=headers, timeout=PROBE_TIMEOUT)
        if r.status_code == 200:
            name = ElementTree.fromstring(r.content).attrib.get('friendlyName')
        elif r.status_code in (401, 403):
            needsAuth = True
    except Exception:
        pass

    return True, name, needsAuth


def addServerDialog():
    """
    Dialog-driven manual server entry with an immediate connection check; on failure
    the entry dialogs are re-offered (values prefilled). Returns True if a server was
    stored.
    """
    ip = ''
    port = '32400'
    token = None
    while True:
        ip = xbmcgui.Dialog().input(T(35023, 'Local server IP or hostname'), ip)
        if not ip:
            return False

        port = xbmcgui.Dialog().input(T(35024, 'Local server port'), port, xbmcgui.INPUT_NUMERIC)
        if not port:
            return False

        token = xbmcgui.Dialog().input(T(35025, 'Plex token (optional)'), token or '') or None

        ok, name, needsAuth = probe(ip, port, token)
        if ok:
            break

        button = xbmcgui.Dialog().yesnocustom(
            T(32427, 'Failed'),
            T(35026, 'Could not reach a Plex Media Server at {0}.').format('{0}:{1}'.format(ip, port)),
            customlabel=T(35033, 'Add anyway'),
            nolabel=T(32337, 'Cancel'),
            yeslabel=T(35032, 'Try again'))
        if button == 1:
            continue
        elif button == 2:
            break
        return False

    if needsAuth:
        xbmcgui.Dialog().ok(
            T(35027, 'Authentication required'),
            T(35028, 'The server requires authentication. Enter a Plex token for it, or add this device\'s '
                     'network to the server\'s "List of IP addresses and networks that are allowed '
                     'without auth" setting.'))

    servers = [s for s in getStoredServers() if s.get('connection') != ip]
    servers.append({'connection': ip, 'port': int(port), 'token': token, 'name': name})
    saveStoredServers(servers)

    util.DEBUG_LOG('Local mode: stored local server {0}:{1} ({2})', ip, port, name or 'unnamed')
    return True


def offerServerIfNoneFound():
    """
    Local mode ended up without any reachable server - offer manual entry.
    Returns True if a server was stored (caller should re-check connections).
    """
    if not xbmcgui.Dialog().yesno(
            T(35030, 'No local server found'),
            T(35031, 'No local Plex Media Server was reachable. Add one by IP address?')):
        return False

    return addServerDialog()


def bootstrap():
    """
    Account-less local mode entry from the pre-signin screen.
    """
    if not addServerDialog():
        return False

    util.setSetting('local_mode', True)
    return True


def seedUsersFromServer(server=None):
    """
    Account-less local mode: seed selectable user profiles from the PMS /accounts endpoint
    (server-side accounts the PMS tracks watch state for). These are profiles, not
    authenticated identities: without per-user tokens the PMS still sees the bootstrap token.
    """
    from plexnet import plexapp, plexrequest, myplexaccount

    account = plexapp.ACCOUNT
    if account.isSignedIn or account.homeUsers:
        return

    server = server or plexapp.SERVERMANAGER.selectedServer
    if not server:
        return

    try:
        req = plexrequest.PlexRequest(server, '/accounts')
        data = ElementTree.fromstring(req.getToStringWithTimeout(PROBE_TIMEOUT))
    except Exception:
        util.DEBUG_LOG('Local mode: no user accounts available from {0}', repr(server.name))
        return

    users = []
    for acc in data.findall('Account'):
        accountID = acc.attrib.get('id')
        # id 0 is the PMS' "unattributed" pseudo account
        if not accountID or accountID == '0':
            continue

        user = myplexaccount.HomeUser({
            'id': accountID,
            'title': acc.attrib.get('name') or accountID,
            'thumb': acc.attrib.get('thumb') or '',
            'admin': accountID == '1' and '1' or '0',
            'restricted': '0',
            'protected': '0',
        })
        user.isAdmin = accountID == '1'
        user.isManaged = False
        user.isProtected = False
        users.append(user)

    if len(users) > 1:
        util.DEBUG_LOG('Local mode: seeded {0} user profiles from {1}', len(users), repr(server.name))
        account.homeUsers = users
        if not account.ID:
            account.ID = '1'
            account.title = users[0].title
        account.saveState()
