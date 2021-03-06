# encoding: utf-8

import logging
import json
import sqlalchemy

import ckan.lib.search as search
import ckan.lib.navl.dictization_functions
import ckan.logic as logic
import ckan.plugins as p
from ckan.common import config
import ckanext.datastore.db as db
import ckanext.datastore.logic.schema as dsschema
import ckanext.datastore.helpers as datastore_helpers

log = logging.getLogger(__name__)
_get_or_bust = logic.get_or_bust
_validate = ckan.lib.navl.dictization_functions.validate

WHITELISTED_RESOURCES = ['_table_metadata']


def datastore_create(context, data_dict):
    '''Adds a new table to the DataStore.

    The datastore_create action allows you to post JSON data to be
    stored against a resource. This endpoint also supports altering tables,
    aliases and indexes and bulk insertion. This endpoint can be called multiple
    times to initially insert more data, add fields, change the aliases or indexes
    as well as the primary keys.

    To create an empty datastore resource and a CKAN resource at the same time,
    provide ``resource`` with a valid ``package_id`` and omit the ``resource_id``.

    If you want to create a datastore resource from the content of a file,
    provide ``resource`` with a valid ``url``.

    See :ref:`fields` and :ref:`records` for details on how to lay out records.

    :param resource_id: resource id that the data is going to be stored against.
    :type resource_id: string
    :param force: set to True to edit a read-only resource
    :type force: bool (optional, default: False)
    :param resource: resource dictionary that is passed to
        :meth:`~ckan.logic.action.create.resource_create`.
        Use instead of ``resource_id`` (optional)
    :type resource: dictionary
    :param aliases: names for read only aliases of the resource. (optional)
    :type aliases: list or comma separated string
    :param fields: fields/columns and their extra metadata. (optional)
    :type fields: list of dictionaries
    :param records: the data, eg: [{"dob": "2005", "some_stuff": ["a", "b"]}]  (optional)
    :type records: list of dictionaries
    :param primary_key: fields that represent a unique key (optional)
    :type primary_key: list or comma separated string
    :param indexes: indexes on table (optional)
    :type indexes: list or comma separated string

    Please note that setting the ``aliases``, ``indexes`` or ``primary_key`` replaces the exising
    aliases or constraints. Setting ``records`` appends the provided records to the resource.

    **Results:**

    :returns: The newly created data object.
    :rtype: dictionary

    See :ref:`fields` and :ref:`records` for details on how to lay out records.

    '''
    schema = context.get('schema', dsschema.datastore_create_schema())
    records = data_dict.pop('records', None)
    resource = data_dict.pop('resource', None)
    data_dict, errors = _validate(data_dict, schema, context)
    resource_dict = None
    if records:
        data_dict['records'] = records
    if resource:
        data_dict['resource'] = resource
    if errors:
        raise p.toolkit.ValidationError(errors)

    p.toolkit.check_access('datastore_create', context, data_dict)

    if 'resource' in data_dict and 'resource_id' in data_dict:
        raise p.toolkit.ValidationError({
            'resource': ['resource cannot be used with resource_id']
        })

    if not 'resource' in data_dict and not 'resource_id' in data_dict:
        raise p.toolkit.ValidationError({
            'resource_id': ['resource_id or resource required']
        })

    if 'resource' in data_dict:
        has_url = 'url' in data_dict['resource']
        # A datastore only resource does not have a url in the db
        data_dict['resource'].setdefault('url', '_datastore_only_resource')
        resource_dict = p.toolkit.get_action('resource_create')(
            context, data_dict['resource'])
        data_dict['resource_id'] = resource_dict['id']

        # create resource from file
        if has_url:
            if not p.plugin_loaded('datapusher'):
                raise p.toolkit.ValidationError({'resource': [
                    'The datapusher has to be enabled.']})
            p.toolkit.get_action('datapusher_submit')(context, {
                'resource_id': resource_dict['id'],
                'set_url_type': True
            })
            # since we'll overwrite the datastore resource anyway, we
            # don't need to create it here
            return

        # create empty resource
        else:
            # no need to set the full url because it will be set in before_show
            resource_dict['url_type'] = 'datastore'
            p.toolkit.get_action('resource_update')(context, resource_dict)
    else:
        if not data_dict.pop('force', False):
            resource_id = data_dict['resource_id']
            _check_read_only(context, resource_id)

    data_dict['connection_url'] = config['ckan.datastore.write_url']

    # validate aliases
    aliases = datastore_helpers.get_list(data_dict.get('aliases', []))
    for alias in aliases:
        if not db._is_valid_table_name(alias):
            raise p.toolkit.ValidationError({
                'alias': [u'"{0}" is not a valid alias name'.format(alias)]
            })

    # create a private datastore resource, if necessary
    model = _get_or_bust(context, 'model')
    resource = model.Resource.get(data_dict['resource_id'])
    legacy_mode = 'ckan.datastore.read_url' not in config
    if not legacy_mode and resource.package.private:
        data_dict['private'] = True

    try:
        result = db.create(context, data_dict)
    except db.InvalidDataError as err:
        raise p.toolkit.ValidationError(unicode(err))

    # Set the datastore_active flag on the resource if necessary
    if resource.extras.get('datastore_active') is not True:
        log.debug(
            'Setting datastore_active=True on resource {0}'.format(resource.id)
        )
        set_datastore_active_flag(model, data_dict, True)

    result.pop('id', None)
    result.pop('private', None)
    result.pop('connection_url')
    return result


def datastore_upsert(context, data_dict):
    '''Updates or inserts into a table in the DataStore

    The datastore_upsert API action allows you to add or edit records to
    an existing DataStore resource. In order for the *upsert* and *update*
    methods to work, a unique key has to be defined via the datastore_create
    action. The available methods are:

    *upsert*
        Update if record with same key already exists, otherwise insert.
        Requires unique key.
    *insert*
        Insert only. This method is faster that upsert, but will fail if any
        inserted record matches an existing one. Does *not* require a unique
        key.
    *update*
        Update only. An exception will occur if the key that should be updated
        does not exist. Requires unique key.


    :param resource_id: resource id that the data is going to be stored under.
    :type resource_id: string
    :param force: set to True to edit a read-only resource
    :type force: bool (optional, default: False)
    :param records: the data, eg: [{"dob": "2005", "some_stuff": ["a","b"]}] (optional)
    :type records: list of dictionaries
    :param method: the method to use to put the data into the datastore.
                   Possible options are: upsert, insert, update (optional, default: upsert)
    :type method: string

    **Results:**

    :returns: The modified data object.
    :rtype: dictionary

    '''
    schema = context.get('schema', dsschema.datastore_upsert_schema())
    records = data_dict.pop('records', None)
    data_dict, errors = _validate(data_dict, schema, context)
    if records:
        data_dict['records'] = records
    if errors:
        raise p.toolkit.ValidationError(errors)

    p.toolkit.check_access('datastore_upsert', context, data_dict)

    if not data_dict.pop('force', False):
        resource_id = data_dict['resource_id']
        _check_read_only(context, resource_id)

    data_dict['connection_url'] = config['ckan.datastore.write_url']

    res_id = data_dict['resource_id']
    resources_sql = sqlalchemy.text(u'''SELECT 1 FROM "_table_metadata"
                                        WHERE name = :id AND alias_of IS NULL''')
    results = db._get_engine(data_dict).execute(resources_sql, id=res_id)
    res_exists = results.rowcount > 0

    if not res_exists:
        raise p.toolkit.ObjectNotFound(p.toolkit._(
            u'Resource "{0}" was not found.'.format(res_id)
        ))

    result = db.upsert(context, data_dict)
    result.pop('id', None)
    result.pop('connection_url')
    return result


def datastore_info(context, data_dict):
    '''
    Returns information about the data imported, such as column names
    and types.

    :rtype: A dictionary describing the columns and their types.
    :param id: Id of the resource we want info about
    :type id: A UUID
    '''
    def _type_lookup(t):
        if t in ['numeric', 'integer']:
            return 'number'

        if t.startswith('timestamp'):
            return "date"

        return "text"

    p.toolkit.check_access('datastore_info', context, data_dict)

    resource_id = _get_or_bust(data_dict, 'id')
    resource = p.toolkit.get_action('resource_show')(context, {'id':resource_id})

    data_dict['connection_url'] = config['ckan.datastore.read_url']

    resources_sql = sqlalchemy.text(u'''SELECT 1 FROM "_table_metadata"
                                        WHERE name = :id AND alias_of IS NULL''')
    results = db._get_engine(data_dict).execute(resources_sql, id=resource_id)
    res_exists = results.rowcount > 0
    if not res_exists:
        raise p.toolkit.ObjectNotFound(p.toolkit._(
            u'Resource "{0}" was not found.'.format(resource_id)
        ))

    info = {'schema': {}, 'meta': {}}

    schema_results = None
    meta_results = None
    try:
        schema_sql = sqlalchemy.text(u'''
            SELECT column_name, data_type
            FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name = :resource_id;
        ''')
        schema_results = db._get_engine(data_dict).execute(schema_sql, resource_id=resource_id)
        for row in schema_results.fetchall():
            k = row[0]
            v = row[1]
            if k.startswith('_'):  # Skip internal rows
                continue
            info['schema'][k] = _type_lookup(v)

        # We need to make sure the resource_id is a valid resource_id before we use it like
        # this, we have done that above.
        meta_sql = sqlalchemy.text(u'''
            SELECT count(_id) FROM "{0}";
        '''.format(resource_id))
        meta_results = db._get_engine(data_dict).execute(meta_sql, resource_id=resource_id)
        info['meta']['count'] = meta_results.fetchone()[0]
    finally:
        if schema_results:
            schema_results.close()
        if meta_results:
            meta_results.close()

    return info


def datastore_delete(context, data_dict):
    '''Deletes a table or a set of records from the DataStore.

    :param resource_id: resource id that the data will be deleted from. (optional)
    :type resource_id: string
    :param force: set to True to edit a read-only resource
    :type force: bool (optional, default: False)
    :param filters: filters to apply before deleting (eg {"name": "fred"}).
                   If missing delete whole table and all dependent views. (optional)
    :type filters: dictionary

    **Results:**

    :returns: Original filters sent.
    :rtype: dictionary

    '''
    schema = context.get('schema', dsschema.datastore_upsert_schema())

    # Remove any applied filters before running validation.
    filters = data_dict.pop('filters', None)
    data_dict, errors = _validate(data_dict, schema, context)

    if filters is not None:
        if not isinstance(filters, dict):
            raise p.toolkit.ValidationError({
                'filters': [
                    'filters must be either a dict or null.'
                ]
            })
        data_dict['filters'] = filters

    if errors:
        raise p.toolkit.ValidationError(errors)

    p.toolkit.check_access('datastore_delete', context, data_dict)

    if not data_dict.pop('force', False):
        resource_id = data_dict['resource_id']
        _check_read_only(context, resource_id)

    data_dict['connection_url'] = config['ckan.datastore.write_url']

    res_id = data_dict['resource_id']
    resources_sql = sqlalchemy.text(u'''SELECT 1 FROM "_table_metadata"
                                        WHERE name = :id AND alias_of IS NULL''')
    results = db._get_engine(data_dict).execute(resources_sql, id=res_id)
    res_exists = results.rowcount > 0

    if not res_exists:
        raise p.toolkit.ObjectNotFound(p.toolkit._(
            u'Resource "{0}" was not found.'.format(res_id)
        ))

    result = db.delete(context, data_dict)

    # Set the datastore_active flag on the resource if necessary
    model = _get_or_bust(context, 'model')
    resource = model.Resource.get(data_dict['resource_id'])

    if (not data_dict.get('filters') and
            resource.extras.get('datastore_active') is True):
        log.debug(
            'Setting datastore_active=False on resource {0}'.format(resource.id)
        )
        set_datastore_active_flag(model, data_dict, False)

    result.pop('id', None)
    result.pop('connection_url')
    return result


@logic.side_effect_free
def datastore_search(context, data_dict):
    '''Search a DataStore resource.

    The datastore_search action allows you to search data in a resource.
    DataStore resources that belong to private CKAN resource can only be
    read by you if you have access to the CKAN resource and send the appropriate
    authorization.

    :param resource_id: id or alias of the resource to be searched against
    :type resource_id: string
    :param filters: matching conditions to select, e.g {"key1": "a", "key2": "b"} (optional)
    :type filters: dictionary
    :param q: full text query. If it's a string, it'll search on all fields on
              each row. If it's a dictionary as {"key1": "a", "key2": "b"},
              it'll search on each specific field (optional)
    :type q: string or dictionary
    :param distinct: return only distinct rows (optional, default: false)
    :type distinct: bool
    :param plain: treat as plain text query (optional, default: true)
    :type plain: bool
    :param language: language of the full text query (optional, default: english)
    :type language: string
    :param limit: maximum number of rows to return (optional, default: 100)
    :type limit: int
    :param offset: offset this number of rows (optional)
    :type offset: int
    :param fields: fields to return (optional, default: all fields in original order)
    :type fields: list or comma separated string
    :param sort: comma separated field names with ordering
                 e.g.: "fieldname1, fieldname2 desc"
    :type sort: string

    Setting the ``plain`` flag to false enables the entire PostgreSQL `full text search query language`_.

    A listing of all available resources can be found at the alias ``_table_metadata``.

    .. _full text search query language: http://www.postgresql.org/docs/9.1/static/datatype-textsearch.html#DATATYPE-TSQUERY

    If you need to download the full resource, read :ref:`dump`.

    **Results:**

    The result of this action is a dictionary with the following keys:

    :rtype: A dictionary with the following keys
    :param fields: fields/columns and their extra metadata
    :type fields: list of dictionaries
    :param offset: query offset value
    :type offset: int
    :param limit: query limit value
    :type limit: int
    :param filters: query filters
    :type filters: list of dictionaries
    :param total: number of total matching records
    :type total: int
    :param records: list of matching results
    :type records: list of dictionaries

    '''
    schema = context.get('schema', dsschema.datastore_search_schema())
    data_dict, errors = _validate(data_dict, schema, context)
    if errors:
        raise p.toolkit.ValidationError(errors)

    res_id = data_dict['resource_id']
    data_dict['connection_url'] = config['ckan.datastore.write_url']

    resources_sql = sqlalchemy.text(u'''SELECT alias_of FROM "_table_metadata"
                                        WHERE name = :id''')
    results = db._get_engine(data_dict).execute(resources_sql, id=res_id)

    # Resource only has to exist in the datastore (because it could be an alias)
    if not results.rowcount > 0:
        raise p.toolkit.ObjectNotFound(p.toolkit._(
            'Resource "{0}" was not found.'.format(res_id)
        ))

    if not data_dict['resource_id'] in WHITELISTED_RESOURCES:
        # Replace potential alias with real id to simplify access checks
        resource_id = results.fetchone()[0]
        if resource_id:
            data_dict['resource_id'] = resource_id

        p.toolkit.check_access('datastore_search', context, data_dict)

    result = db.search(context, data_dict)
    result.pop('id', None)
    result.pop('connection_url')
    return result


@logic.side_effect_free
def datastore_search_sql(context, data_dict):
    '''Execute SQL queries on the DataStore.

    The datastore_search_sql action allows a user to search data in a resource
    or connect multiple resources with join expressions. The underlying SQL
    engine is the
    `PostgreSQL engine <http://www.postgresql.org/docs/9.1/interactive/>`_.
    There is an enforced timeout on SQL queries to avoid an unintended DOS.
    DataStore resource that belong to a private CKAN resource cannot be searched with
    this action. Use :meth:`~ckanext.datastore.logic.action.datastore_search` instead.

    .. note:: This action is only available when using PostgreSQL 9.X and using a read-only user on the database.
        It is not available in :ref:`legacy mode<legacy-mode>`.

    :param sql: a single SQL select statement
    :type sql: string

    **Results:**

    The result of this action is a dictionary with the following keys:

    :rtype: A dictionary with the following keys
    :param fields: fields/columns and their extra metadata
    :type fields: list of dictionaries
    :param records: list of matching results
    :type records: list of dictionaries

    '''
    sql = _get_or_bust(data_dict, 'sql')

    if not datastore_helpers.is_single_statement(sql):
        raise p.toolkit.ValidationError({
            'query': ['Query is not a single statement.']
        })

    p.toolkit.check_access('datastore_search_sql', context, data_dict)

    data_dict['connection_url'] = config['ckan.datastore.read_url']

    result = db.search_sql(context, data_dict)
    result.pop('id', None)
    result.pop('connection_url')
    return result


def datastore_make_private(context, data_dict):
    ''' Deny access to the DataStore table through
    :meth:`~ckanext.datastore.logic.action.datastore_search_sql`.

    This action is called automatically when a CKAN dataset becomes
    private or a new DataStore table is created for a CKAN resource
    that belongs to a private dataset.

    :param resource_id: id of resource that should become private
    :type resource_id: string
    '''
    if 'id' in data_dict:
        data_dict['resource_id'] = data_dict['id']
    res_id = _get_or_bust(data_dict, 'resource_id')

    data_dict['connection_url'] = config['ckan.datastore.write_url']

    if not _resource_exists(context, data_dict):
        raise p.toolkit.ObjectNotFound(p.toolkit._(
            u'Resource "{0}" was not found.'.format(res_id)
        ))

    p.toolkit.check_access('datastore_change_permissions', context, data_dict)

    db.make_private(context, data_dict)


def datastore_make_public(context, data_dict):
    ''' Allow access to the DataStore table through
    :meth:`~ckanext.datastore.logic.action.datastore_search_sql`.

    This action is called automatically when a CKAN dataset becomes
    public.

    :param resource_id: if of resource that should become public
    :type resource_id: string
    '''
    if 'id' in data_dict:
        data_dict['resource_id'] = data_dict['id']
    res_id = _get_or_bust(data_dict, 'resource_id')

    data_dict['connection_url'] = config['ckan.datastore.write_url']

    if not _resource_exists(context, data_dict):
        raise p.toolkit.ObjectNotFound(p.toolkit._(
            u'Resource "{0}" was not found.'.format(res_id)
        ))

    p.toolkit.check_access('datastore_change_permissions', context, data_dict)

    db.make_public(context, data_dict)


def set_datastore_active_flag(model, data_dict, flag):
    '''
    Set appropriate datastore_active flag on CKAN resource.

    Called after creation or deletion of DataStore table.
    '''
    # We're modifying the resource extra directly here to avoid a
    # race condition, see issue #3245 for details and plan for a
    # better fix
    update_dict = {'datastore_active': flag}

    # get extras(for entity update) and package_id(for search index update)
    res_query = model.Session.query(
        model.resource_table.c.extras,
        model.resource_table.c.package_id
    ).filter(
        model.Resource.id == data_dict['resource_id']
    )
    extras, package_id = res_query.one()

    # update extras in database for record and its revision
    extras.update(update_dict)
    res_query.update({'extras': extras}, synchronize_session=False)
    model.Session.query(model.resource_revision_table).filter(
        model.ResourceRevision.id == data_dict['resource_id'],
        model.ResourceRevision.current is True
    ).update({'extras': extras}, synchronize_session=False)

    model.Session.commit()

    # get package with  updated resource from solr
    # find changed resource, patch it and reindex package
    psi = search.PackageSearchIndex()
    solr_query = search.PackageSearchQuery()
    q = {
        'q': 'id:"{0}"'.format(package_id),
        'fl': 'data_dict',
        'wt': 'json',
        'fq': 'site_id:"%s"' % config.get('ckan.site_id'),
        'rows': 1
    }
    for record in solr_query.run(q)['results']:
        solr_data_dict = json.loads(record['data_dict'])
        for resource in solr_data_dict['resources']:
            if resource['id'] == data_dict['resource_id']:
                resource.update(update_dict)
                psi.index_package(solr_data_dict)
                break


def _resource_exists(context, data_dict):
    ''' Returns true if the resource exists in CKAN and in the datastore '''
    model = _get_or_bust(context, 'model')
    res_id = _get_or_bust(data_dict, 'resource_id')
    if not model.Resource.get(res_id):
        return False

    resources_sql = sqlalchemy.text(u'''SELECT 1 FROM "_table_metadata"
                                        WHERE name = :id AND alias_of IS NULL''')
    results = db._get_engine(data_dict).execute(resources_sql, id=res_id)
    return results.rowcount > 0


def _check_read_only(context, resource_id):
    ''' Raises exception if the resource is read-only.
    Make sure the resource id is in resource_id
    '''
    res = p.toolkit.get_action('resource_show')(
        context, {'id': resource_id})
    if res.get('url_type') != 'datastore':
        raise p.toolkit.ValidationError({
            'read-only': ['Cannot edit read-only resource. Either pass'
                          '"force=True" or change url-type to "datastore"']
        })
