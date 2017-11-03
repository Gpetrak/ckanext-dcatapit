import os
import logging
from ConfigParser import SafeConfigParser as ConfigParser

from ckan.plugins import toolkit

from ckan.lib.base import config
from ckan.model import Session, repo
from ckan.model.group import Group, Member


log = logging.getLogger(__name__)

"""
Theme to Group mapping
======================

This allows to automatically assing Groups to Dataset based on used themes. 
This will work for harvested Datasets and with Datasets created with web ui.


Configuration
-------------

 * add `dcatapit_theme_group_mapper` plugin to `ckan.plugins`

 * set `ckanext.dcatapit.theme_group_mapping.file` - path to mapping file. 
        See below for contents

 * set `ckanext.dcatapit.theme_group_mapping.add_new_groups` to `true` if you want to 
        enable automatic Group creation if one is missing, but it's defined in mapping

Mapping file
------------

Mapping file is .ini-style map configuration. Contents should be following:

 * it should have  `dcatapit:theme_group_mapping` section

 * each key is name of theme
 
 * value is a list of groups to assign to. It can be separated with comas or each item can be in new line.

Sample file contents:

.. code::

 [dcatapit:theme_group_mapping]
 thememap1 = somegroup1
     somegroup2, existing-group

If file is specified in configuration, but is not accessible by ckan, warning will be logged.

"""


MAPPING_SECTION = 'dcatapit:theme_group_mapping'
DCATAPIT_THEME_TO_MAPPING_SOURCE = 'ckanext.dcatapit.theme_group_mapping.file'
DCATAPIT_THEME_TO_MAPPING_ADD_NEW_GROUPS = 'ckanext.dcatapit.theme_group_mapping.add_new_groups'


def get_theme_to_groups():
    """
    Returns dictionary with groups for themes
    """
    fname = config.get(DCATAPIT_THEME_TO_MAPPING_SOURCE)
    if not fname:
        return
    if not os.path.exists(fname):
        log.warning("Cannot parse theme mapping, no such file: %s", fname)
        return
    return import_theme_to_group(fname)


def _clean_groups(package):
    """
    Clears package's groups
    """
    Session.query(Member).filter(Member.table_name == 'package',
                                 Member.table_id == package.id,
                                 Member.capacity != 'admin')\
                         .update({'state':'deleted'})


def _add_groups(package_id, groups):
    """
    Adds groups to package
    """
    for g in groups:
        if g.id is None:
            raise ValueError("No id in group %s" % g)

        q = Session.query(Member).filter_by(state='active',
                                           table_id=package_id,
                                           group_id=g.id,
                                           table_name='package')
        # this group is already added to package, skipping
        # note: this will work with groups flushed to db
        if Session.query(q.exists()).scalar():
            continue
        
        member = Member(state='active',
                        table_id=package_id,
                        group_id=g.id,
                        table_name='package')
        Session.add(member)


def _get_group_from_session(gname):
    """
    If Group was created within current session, get
    it from cache instead of db.

    This exists because new, uncommited/unflushed objects are 
    not accessible by Session.query.
    """
    for obj in Session.new:
        if isinstance(obj, Group):
            if obj.name == gname:
                return obj


def populate_theme_groups(instance, clean_existing=False):
    """
    For given instance, it finds groups from mapping corresponding to
    Dataset's themes, and will assign dataset to those groups.

    Existing groups will be removed, if clean_existing is set to True.

    This utilizes `ckanext.dcatapit.theme_group_mapping.add_new_groups`
    configuration option. If it's set to true, and mapped group doesn't exist,
    new group will be created.
    """
    add_new = toolkit.asbool(config.get(DCATAPIT_THEME_TO_MAPPING_ADD_NEW_GROUPS))
    themes = []
    for ex in instance['extras']:
        if ex['key'] == 'theme':
            _t = ex['value']
            if isinstance(_t, list):
                themes.extend(_t)
            else:
                themes.extend([theme for theme in _t.strip('{}').split(',') if theme])

    #themes = instance.extras.get('theme')
    if not themes:
        log.debug("no theme from %s", instance)
        return instance
    theme_map = get_theme_to_groups()
    if not theme_map:
        log.warning("Theme to group map is empty")
        return instance
    if not isinstance(themes, list):
        themes = [themes]
    all_groups = set()
    for theme in themes:
        _groups = theme_map.get(theme)
        if not _groups:
            continue
        all_groups = all_groups.union(set(_groups))

    if clean_existing:
        _clean_groups(instance)
    groups = []
    for gname in all_groups:
        gname = gname.strip()
        if not gname:
            continue
        group = Group.get(gname) or _get_group_from_session(gname)
        if add_new and group is None:
            group = Group(name=gname)
            Session.add(group)
        if group:
            groups.append(group)
    
    if Session.new:
        # flush to db, refresh with ids
        rev = Session.revision
        Session.flush()
        Session.revision = rev
        groups = [(Group.get(g.name) if g.id is None else g) for g in groups]
    
    _add_groups(instance['id'], set(groups))
    
    # preserve revision, since it's not a commit yet
    rev = Session.revision
    Session.flush()
    Session.revision = rev

    return instance


def import_theme_to_group(fname):
    """
    Import theme to group mapping configuration from path

    Function will parse .ini file and populate mapping tables. 

    This function will make commits internally, so caller should create fresh revision before commiting later.

    Sample configuration file:

[dcatapit:theme_group_mapping]

# can be one line of values separated by coma
Economy = economy01, economy02, test01, test02

# can be per-line list
Society = society
    economy01
    other02

# or mixed
OP_DATPRO = test01
    test02, test03, dupatest

    """
    fpath = os.path.abspath(fname)
    conf = ConfigParser()
    # otherwise theme names will be lower-cased
    conf.optionxform = str
    conf.read([fpath])
    if not conf.has_section(MAPPING_SECTION):
        log.warning("Theme to groups mapping config: cannot find %s section in %s",
                    MAPPING_SECTION, fpath)
        return
    out = {}
    for theme_name, groups in conf.items(MAPPING_SECTION, raw=True):
        out[theme_name] = groups.replace('\n', ',').split(',')
    log.info("Read theme to groups mapping definition from %s. %s themes to map.", fpath, len(out.keys()))
    return out