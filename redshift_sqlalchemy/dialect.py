from collections import defaultdict
import numbers
import pkg_resources
import re

import sqlalchemy as sa
from sqlalchemy import schema, exc, inspect, Column
from sqlalchemy.dialects.postgresql.base import PGDDLCompiler, PGCompiler
from sqlalchemy.dialects.postgresql.psycopg2 import PGDialect_psycopg2
from sqlalchemy.engine import reflection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import BindParameter, Executable, ClauseElement
from sqlalchemy.types import VARCHAR, NullType


try:
    from alembic.ddl import postgresql
except ImportError:
    pass
else:
    from alembic.ddl.base import RenameTable
    compiles(RenameTable, 'redshift')(postgresql.visit_rename_table)

    class RedshiftImpl(postgresql.PostgresqlImpl):
        __dialect__ = 'redshift'


# Regex for parsing and identity constraint out of adsrc, e.g.:
#   "identity"(445178, 0, '1,1'::text)
IDENTITY_RE = re.compile(r"""
    "identity" \(
      (?P<current>-?\d+)
      ,\s
      (?P<base>-?\d+)
      ,\s
      '(?P<seed>-?\d+),(?P<step>-?\d+)'
      .*
    \)
""", re.VERBOSE)

# Regex for SQL identifiers (valid table and column names)
SQL_IDENTIFIER_RE = re.compile(r"""
   [_a-zA-Z][\w$]*  # SQL standard identifier
   |                # or
   (?:"[^"]+")+     # SQL delimited (quoted) identifier
""", re.VERBOSE)

# Regex for foreign key constraints, e.g.:
#   FOREIGN KEY(col1) REFERENCES othertable (col2)
# See https://docs.aws.amazon.com/redshift/latest/dg/r_names.html
# for a definition of valid SQL identifiers.
FOREIGN_KEY_RE = re.compile(r"""
  ^FOREIGN\ KEY \s* \(   # FOREIGN KEY, arbitrary whitespace, literal '('
    (?P<columns>         # Start a group to capture the referring columns
      (?:                # Start a non-capturing group
        \s*              # Arbitrary whitespace
        ([_a-zA-Z][\w$]* | ("[^"]+")+)  # SQL identifier
        \s*              # Arbitrary whitespace
        ,?               # There will be a colon if this isn't the last one
      )+                 # Close the non-capturing group; require at least one
    )                    # Close the 'columns' group
  \s* \)                 # Arbitrary whitespace and literal ')'
  \s* REFERENCES \s*
  (?P<referred_table>    # Start a group to capture the referred table name
    ([_a-zA-Z][\w$]* | ("[^"]*")+)      # SQL identifier
  )
  \s* \( \s*             # Literal '(' surrounded by arbitrary whitespace
    (?P<referred_column> # Start a group to capture the referred column name
      ([_a-zA-Z][\w$]* | ("[^"]*")+)    # SQL identifier
    )
  \s* \)                 # Arbitrary whitespace and literal ')'
""", re.VERBOSE)

# Regex for primary key constraints, e.g.:
#   PRIMARY KEY (col1, col2)
PRIMARY_KEY_RE = re.compile(r"""
  ^PRIMARY \s* KEY \s* \(  # FOREIGN KEY, arbitrary whitespace, literal '('
    (?P<columns>         # Start a group to capture column names
      (?:
        \s*                # Arbitrary whitespace
        ( [_a-zA-Z][\w$]* | ("[^"]*")+ )  # SQL identifier or delimited identifier
        \s*                # Arbitrary whitespace
        ,?                 # There will be a colon if this isn't the last one
      )+                  # Close the non-capturing group; require at least one
    )
  \s* \) \s*                # Arbitrary whitespace and literal ')'
""", re.VERBOSE)


def _get_relation_key(name, schema):
    if schema is None:
        return name
    else:
        return schema + "." + name


def _get_schema_and_relation(key):
    if '.' not in key:
        return (None, key)
    identifiers = SQL_IDENTIFIER_RE.findall(key)
    if len(identifiers) == 1:
        return (None, key)
    elif len(identifiers) == 2:
        return identifiers
    raise ValueError("%s does not look like a valid relation identifier")


def unquoted(key):
    """
    Return *key* with one level of double quotes removed.

    Redshift stores some identifiers without quotes in internal tables,
    even though the name must be quoted elsewhere.
    In particular, this happens for tables named as a keyword.
    """
    if key.startswith('"') and key.endswith('"'):
        return key[1:-1]
    return key


class RedshiftCompiler(PGCompiler):

    def visit_now_func(self, fn, **kw):
        return "SYSDATE"


class RedShiftDDLCompiler(PGDDLCompiler):
    """
    Handles Redshift-specific CREATE TABLE syntax.

    Users can specify the DISTSTYLE, DISTKEY, SORTKEY and ENCODE properties per
    table and per column.

    Table level properties can be set using the dialect specific syntax. For
    example, to specify a distribution key and style you apply the following ::

    >>> import sqlalchemy as sa
    >>> from sqlalchemy.schema import CreateTable
    >>> engine = sa.create_engine('redshift+psycopg2://example')
    >>> metadata = sa.MetaData()
    >>> user = sa.Table(
    ...     'user',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column('name', sa.String),
    ...     redshift_diststyle='KEY',
    ...     redshift_distkey='id',
    ...     redshift_interleaved_sortkey=['id', 'name'],
    ... )
    >>> print(CreateTable(user).compile(engine))
    <BLANKLINE>
    CREATE TABLE "user" (
        id INTEGER NOT NULL,
        name VARCHAR,
        PRIMARY KEY (id)
    ) DISTSTYLE KEY DISTKEY (id) INTERLEAVED SORTKEY (id, name)
    <BLANKLINE>
    <BLANKLINE>

    A single sort key can be applied without a wrapping list ::

    >>> customer = sa.Table(
    ...     'customer',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column('name', sa.String),
    ...     redshift_sortkey='id',
    ... )
    >>> print(CreateTable(customer).compile(engine))
    <BLANKLINE>
    CREATE TABLE customer (
        id INTEGER NOT NULL,
        name VARCHAR,
        PRIMARY KEY (id)
    ) SORTKEY (id)
    <BLANKLINE>
    <BLANKLINE>

    Column-level special syntax can also be applied using the column info
    dictionary. For example, we can specify the ENCODE for a column ::

    >>> product = sa.Table(
    ...     'product',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column('name', sa.String, info={'encode': 'lzo'})
    ... )
    >>> print(CreateTable(product).compile(engine))
    <BLANKLINE>
    CREATE TABLE product (
        id INTEGER NOT NULL,
        name VARCHAR ENCODE lzo,
        PRIMARY KEY (id)
    )
    <BLANKLINE>
    <BLANKLINE>

    We can also specify the distkey and sortkey options ::

    >>> sku = sa.Table(
    ...     'sku',
    ...     metadata,
    ...     sa.Column('id', sa.Integer, primary_key=True),
    ...     sa.Column(
    ...         'name', sa.String, info={'distkey': True, 'sortkey': True}
    ...     )
    ... )
    >>> print(CreateTable(sku).compile(engine))
    <BLANKLINE>
    CREATE TABLE sku (
        id INTEGER NOT NULL,
        name VARCHAR DISTKEY SORTKEY,
        PRIMARY KEY (id)
    )
    <BLANKLINE>
    <BLANKLINE>
    """

    def post_create_table(self, table):
        text = ""
        info = table.dialect_options['redshift']

        diststyle = info.get('diststyle')
        if diststyle:
            diststyle = diststyle.upper()
            if diststyle not in ('EVEN', 'KEY', 'ALL'):
                raise exc.CompileError(
                    u"diststyle {0} is invalid".format(diststyle)
                )
            text += " DISTSTYLE " + diststyle

        distkey = info.get('distkey')
        if distkey:
            text += " DISTKEY ({0})".format(distkey)

        sortkey = info.get('sortkey')
        interleaved_sortkey = info.get('interleaved_sortkey')
        if sortkey and interleaved_sortkey:
            raise exc.ArgumentError(
                "Parameters sortkey and interleaved_sortkey are "
                "mutually exclusive; you may not specify both."
            )
        if sortkey or interleaved_sortkey:
            if isinstance(sortkey, str):
                keys = [sortkey]
            else:
                keys = sortkey or interleaved_sortkey
            keys = [key.name if isinstance(key, Column) else key
                    for key in keys]
            if interleaved_sortkey:
                text += " INTERLEAVED"
            text += " SORTKEY ({0})".format(", ".join(keys))
        return text

    def get_column_specification(self, column, **kwargs):
        colspec = self.preparer.format_column(column)

        colspec += " " + self.dialect.type_compiler.process(column.type)

        default = self.get_column_default_string(column)
        if default is not None:
            # Identity constraints show up as *default* when reflected.
            m = IDENTITY_RE.match(default)
            if m:
                colspec += " IDENTITY({seed},{step})".format(**m.groupdict())
            else:
                colspec += " DEFAULT " + default

        colspec += self._fetch_redshift_column_attributes(column)

        if not column.nullable:
            colspec += " NOT NULL"
        return colspec

    def _fetch_redshift_column_attributes(self, column):
        text = ""
        if not hasattr(column, 'info'):
            return text
        info = column.info
        identity = info.get('identity')
        if identity:
            text += " IDENTITY({0},{1})".format(identity[0], identity[1])

        encode = info.get('encode')
        if encode:
            text += " ENCODE " + encode

        distkey = info.get('distkey')
        if distkey:
            text += " DISTKEY"

        sortkey = info.get('sortkey')
        if sortkey:
            text += " SORTKEY"
        return text


class RedshiftDialect(PGDialect_psycopg2):
    """
    Define Redshift-specific behavior.

    Most public methods are overrides of the underlying interfaces defined in
    :class:`~sqlalchemy.engine.interfaces.Dialect` and
    :class:`~sqlalchemy.engine.Inspector`.
    """

    name = 'redshift'

    statement_compiler = RedshiftCompiler
    ddl_compiler = RedShiftDDLCompiler

    construct_arguments = [
        (schema.Index, {
            "using": False,
            "where": None,
            "ops": {}
        }),
        (schema.Table, {
            "ignore_search_path": False,
            "diststyle": None,
            "distkey": None,
            "sortkey": None,
            "interleaved_sortkey": None,
        }),
    ]

    def __init__(self, *args, **kw):
        super(RedshiftDialect, self).__init__(*args, **kw)
        # Cache domains, as these will be static;
        # Redshift does not support user-created domains.
        self._domains = None

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        """
        Return information about columns in `table_name`.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.get_columns`.
        """
        cols = self._get_redshift_columns(connection, table_name, schema, **kw)
        if not self._domains:
            self._domains = self._load_domains(connection)
        domains = self._domains
        columns = []
        for col in cols:
            column_info = self._get_column_info(
                name=col.name, format_type=col.format_type,
                default=col.default, notnull=col.notnull, domains=domains,
                enums=[], schema=col.schema, encode=col.encode)
            columns.append(column_info)
        return columns

    @reflection.cache
    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        """
        Return information about the primary key constraint on `table_name`.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.get_pk_constraint`.
        """
        constraints = self._get_redshift_constraints(connection, table_name,
                                                     schema)
        pk_constraints = [c for c in constraints if c.contype == 'p']
        if not pk_constraints:
            return {'constrained_columns': [], 'name': ''}
        pk_constraint = pk_constraints[0]
        m = PRIMARY_KEY_RE.match(pk_constraint.condef)
        colstring = m.group('columns')
        constrained_columns = SQL_IDENTIFIER_RE.findall(colstring)
        return {
            'constrained_columns': constrained_columns,
            'name': None,
        }

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        """
        Return information about foreign keys in `table_name`.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.get_pk_constraint`.
        """
        constraints = self._get_redshift_constraints(connection, table_name,
                                                     schema)
        fk_constraints = [c for c in constraints if c.contype == 'f']
        fkeys = []
        for constraint in fk_constraints:
            m = FOREIGN_KEY_RE.match(constraint.condef)
            referred_column = m.group('referred_column')
            referred_columns = [referred_column]
            referred_table = m.group('referred_table')
            referred_table, _, referred_schema = referred_table.partition('.')
            colstring = m.group('columns')
            constrained_columns = SQL_IDENTIFIER_RE.findall(colstring)
            fkey_d = {
                'name': None,
                'constrained_columns': constrained_columns,
                'referred_schema': referred_schema or None,
                'referred_table': referred_table,
                'referred_columns': referred_columns,
            }
            fkeys.append(fkey_d)
        return fkeys

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        """
        Return a list of table names for `schema`.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.get_table_names`.
        """
        default_schema = inspect(connection).default_schema_name
        if not schema:
            schema = default_schema
        info_cache = kw.get('info_cache')
        all_tables, _ = self._get_all_table_and_view_info(connection,
                                                          info_cache=info_cache)
        table_names = []
        for key in all_tables.keys():
            this_schema, this_table = _get_schema_and_relation(key)
            if this_schema is None:
                this_schema = default_schema
            if this_schema == schema:
                table_names.append(this_table)
        return table_names

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        """
        Return a list of all view names available in the database.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.get_view_names`.
        """
        default_schema = inspect(connection).default_schema_name
        if not schema:
            schema = self.dialect.default_schema_name
        info_cache = kw.get('info_cache')
        _, all_views = self._get_all_table_and_view_info(connection,
                                                         info_cache=info_cache)
        view_names = []
        for key in all_views.keys():
            this_schema, this_view = _get_schema_and_relation(key)
            if this_schema is None:
                this_schema = default_schema
            if this_schema == schema:
                view_names.append(this_view)
        return view_names

    @reflection.cache
    def get_view_definition(self, connection, view_name, schema=None, **kw):
        """Return view definition.
        Given a :class:`.Connection`, a string `view_name`,
        and an optional string `schema`, return the view definition.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.get_view_definition`.
        """
        view = self._get_redshift_view(connection, view_name, schema, **kw)
        return view.view_definition

    def get_indexes(self, connection, table_name, schema, **kw):
        """
        Return information about indexes in `table_name`.

        Because Redshift does not support traditional indexes,
        this always returns an empty list.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.get_indexes`.
        """
        return []

    @reflection.cache
    def get_unique_constraints(self, connection, table_name,
                               schema=None, **kw):
        """
        Return information about unique constraints in `table_name`.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.get_unique_constraints`.
        """
        constraints = self._get_redshift_constraints(connection,
                                                     table_name, schema)
        constraints = [c for c in constraints if c.contype == 'u']
        uniques = defaultdict(lambda: defaultdict(dict))
        for con in constraints:
            uniques[con.conname]["key"] = con.conkey
            uniques[con.conname]["cols"][con.attnum] = con.attname

        return [
            {'name': None,
             'column_names': [uc["cols"][i] for i in uc["key"]]}
            for name, uc in uniques.items()
        ]

    @reflection.cache
    def get_table_options(self, connection, table_name, schema, **kw):
        """
        Return a dictionary of options specified when the table of the
        given name was created.

        See :meth:`~sqlalchemy.engine.Inspector.get_table_options`.
        """
        def keyfunc(column):
            num = int(column.sortkey)
            # If sortkey is interleaved, column numbers alternate
            # negative values, so take abs.
            return abs(num)
        table = self._get_redshift_table(connection, table_name,
                                         schema, **kw)
        columns = self._get_redshift_columns(connection, table_name,
                                             schema, **kw)
        sortkey_cols = sorted([col for col in columns if col.sortkey],
                              key=keyfunc)
        interleaved = any([int(col.sortkey) < 0 for col in sortkey_cols])
        sortkey = [col.name for col in sortkey_cols]
        interleaved_sortkey = None
        if interleaved:
            interleaved_sortkey = sortkey
            sortkey = None
        distkeys = [col.name for col in columns if col.distkey]
        distkey = distkeys[0] if distkeys else None
        return {
            'redshift_diststyle': table.diststyle,
            'redshift_distkey': distkey,
            'redshift_sortkey': sortkey,
            'redshift_interleaved_sortkey': interleaved_sortkey,
        }

    def create_connect_args(self, *args, **kwargs):
        """
        Build DB-API compatible connection arguments.

        See :meth:`~sqlalchemy.engine.interfaces.Dialect.create_connect_args`.
        """
        default_args = {
            'sslmode': 'verify-full',
            'sslrootcert': pkg_resources.resource_filename(
                __name__,
                'redshift-ssl-ca-cert.pem'
            ),
        }
        cargs, cparams = super(RedshiftDialect, self).create_connect_args(
            *args, **kwargs
        )
        default_args.update(cparams)
        return cargs, default_args

    def _get_column_info(self, *args, **kwargs):
        kw = kwargs.copy()
        encode = kw.pop('encode', None)
        column_info = super(RedshiftDialect, self)._get_column_info(
            *args,
            **kw
        )
        if isinstance(column_info['type'], VARCHAR):
            if column_info['type'].length is None:
                column_info['type'] = NullType()
        if 'info' not in column_info:
            column_info['info'] = {}
        if encode and encode != 'none':
            column_info['info']['encode'] = encode
        return column_info

    def _get_redshift_table(self, connection, table_name, schema=None, **kw):
        info_cache = kw.get('info_cache')
        all_tables, _ = self._get_all_table_and_view_info(
            connection, info_cache=info_cache)
        key = _get_relation_key(table_name, schema)
        if key not in all_tables.keys():
            key = unquoted(key)
        return all_tables[key]

    def _get_redshift_view(self, connection, view_name, schema=None, **kw):
        info_cache = kw.get('info_cache')
        _, all_views = self._get_all_table_and_view_info(connection,
                                                         info_cache=info_cache)
        key = _get_relation_key(view_name, schema)
        if key not in all_views.keys():
            key = unquoted(key)
        return all_views[key]

    def _get_redshift_columns(self, connection, table_name, schema=None, **kw):
        info_cache = kw.get('info_cache')
        all_columns = self._get_all_column_info(connection,
                                                info_cache=info_cache)
        key = _get_relation_key(table_name, schema)
        if key not in all_columns.keys():
            key = unquoted(key)
        return all_columns[key]

    def _get_redshift_constraints(self, connection, table_name,
                                  schema=None, **kw):
        info_cache = kw.get('info_cache')
        all_constraints = self._get_all_constraint_info(connection,
                                                        info_cache=info_cache)
        key = _get_relation_key(table_name, schema)
        if key not in all_constraints.keys():
            key = unquoted(key)
        return all_constraints[key]

    @reflection.cache
    def _get_all_table_and_view_info(self, connection, **kw):
        result = connection.execute("""
        SELECT
          c.relkind,
          n.oid as "schema_oid",
          n.nspname as "schema",
          c.oid as "rel_oid",
          c.relname,
          CASE c.reldiststyle
            WHEN 0 THEN 'EVEN' WHEN 1 THEN 'KEY' WHEN 8 THEN 'ALL' END
            AS "diststyle",
          c.relowner AS "owner_id",
          u.usename AS "owner_name",
          pg_get_viewdef(c.oid) AS "view_definition",
          pg_catalog.array_to_string(c.relacl, '\n') AS "privileges"
        FROM pg_catalog.pg_class c
             LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             JOIN pg_catalog.pg_user u ON u.usesysid = c.relowner
        WHERE c.relkind IN ('r', 'v', 'm', 'S', 'f')
          AND n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
        ORDER BY c.relkind, n.oid, n.nspname;
        """)
        tables, views = {}, {}
        for rel in result:
            schema = rel.schema
            if schema == inspect(connection).default_schema_name:
                schema = None
            key = _get_relation_key(rel.relname, schema)
            if rel.relkind == 'r':
                tables[key] = rel
            if rel.relkind == 'v':
                views[key] = rel
        self._all_tables_and_views = (tables, views)
        return self._all_tables_and_views

    @reflection.cache
    def _get_all_column_info(self, connection, **kw):
        result = connection.execute("""
        SELECT
          n.nspname as "schema",
          c.relname as "table_name",
          d.column as "name",
          encoding as "encode",
          type, distkey, sortkey, "notnull", adsrc, attnum,
          pg_catalog.format_type(att.atttypid, att.atttypmod),
          pg_catalog.pg_get_expr(ad.adbin, ad.adrelid) AS DEFAULT,
          n.oid as "schema_oid",
          c.oid as "table_oid"
        FROM pg_catalog.pg_class c
        LEFT JOIN pg_catalog.pg_namespace n
          ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_table_def d
          ON (d.schemaname, d.tablename) = (n.nspname, c.relname)
        JOIN pg_catalog.pg_attribute att
          ON (att.attrelid, att.attname) = (c.oid, d.column)
        LEFT JOIN pg_catalog.pg_attrdef ad
          ON (att.attrelid, att.attnum) = (ad.adrelid, ad.adnum)
        WHERE n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
        ORDER BY n.nspname, c.relname
        """)
        all_columns = defaultdict(list)
        for col in result:
            schema = col.schema
            if schema == inspect(connection).default_schema_name:
                schema = None
            key = _get_relation_key(col.table_name, schema)
            all_columns[key].append(col)
        self._all_columns = all_columns
        return self._all_columns

    @reflection.cache
    def _get_all_constraint_info(self, connection, **kw):
        result = connection.execute("""
        SELECT
          n.nspname as "schema",
          c.relname as "table_name",
          t.contype,
          t.conname,
          t.conkey,
          a.attnum,
          a.attname,
          pg_catalog.pg_get_constraintdef(t.oid, true) as condef,
          n.oid as "schema_oid",
          c.oid as "rel_oid"
        FROM pg_catalog.pg_class c
        LEFT JOIN pg_catalog.pg_namespace n
          ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_constraint t
          ON t.conrelid = c.oid
        JOIN pg_catalog.pg_attribute a
          ON t.conrelid = a.attrelid AND a.attnum = ANY(t.conkey)
        WHERE n.nspname !~ '^pg_' AND pg_catalog.pg_table_is_visible(c.oid)
        ORDER BY n.nspname, c.relname
        """)
        all_constraints = defaultdict(list)
        for con in result:
            schema = con.schema
            if schema == inspect(connection).default_schema_name:
                schema = None
            key = _get_relation_key(con.table_name, schema)
            all_constraints[key].append(con)
        self._all_constraints = all_constraints
        return self._all_constraints


class UnloadFromSelect(Executable, ClauseElement):
    ''' Prepares a RedShift unload statement to drop a query to Amazon S3
    http://docs.aws.amazon.com/redshift/latest/dg/r_UNLOAD_command_examples.html
    '''
    def __init__(self, select, unload_location, access_key, secret_key, session_token='', options={}):
        ''' Initializes an UnloadFromSelect instance

        Args:
            self: An instance of UnloadFromSelect
            select: The select statement to be unloaded
            unload_location: The Amazon S3 bucket where the result will be stored
            access_key - AWS Access Key (required)
            secret_key - AWS Secret Key (required)
            session_token - AWS STS Session Token (optional)
            options - Set of optional parameters to modify the UNLOAD sql
                parallel: If 'ON' the result will be written to multiple files. If
                    'OFF' the result will write to one (1) file up to 6.2GB before
                    splitting
                add_quotes: Boolean value for ADDQUOTES; defaults to True
                null_as: optional string that represents a null value in unload output
                delimiter - File delimiter. Defaults to ','
        '''
        self.select = select
        self.unload_location = unload_location
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.options = options


@compiles(UnloadFromSelect)
def visit_unload_from_select(element, compiler, **kw):
    ''' Returns the actual sql query for the UnloadFromSelect class
    '''
    return """
           UNLOAD ('%(query)s') TO '%(unload_location)s'
           CREDENTIALS 'aws_access_key_id=%(access_key)s;aws_secret_access_key=%(secret_key)s%(session_token)s'
           DELIMITER '%(delimiter)s'
           %(add_quotes)s
           %(null_as)s
           ALLOWOVERWRITE
           PARALLEL %(parallel)s;
           """ % \
           {'query': compiler.process(element.select, unload_select=True, literal_binds=True),
            'unload_location': element.unload_location,
            'access_key': element.access_key,
            'secret_key': element.secret_key,
            'session_token': ';token=%s' % element.session_token if element.session_token else '',
            'add_quotes': 'ADDQUOTES' if bool(element.options.get('add_quotes', True)) else '',
            'null_as': ("NULL '%s'" % element.options.get('null_as')) if element.options.get('null_as') else '',
            'delimiter': element.options.get('delimiter', ','),
            'parallel': element.options.get('parallel', 'ON')}


# At the time of this implementation, no specification for a session token was
# found. After looking at a few session tokens they appear to be the same as
# the aws_secret_access_key pattern, but much longer. An example token can be
# found here: http://docs.aws.amazon.com/STS/latest/APIReference/API_GetSessionToken.html
# The regexs for access keys can be found here: http://blogs.aws.amazon.com/security/blog/tag/key+rotation
creds_rx = re.compile(r"""
    ^aws_access_key_id=[A-Z0-9]{20};
    aws_secret_access_key=[A-Za-z0-9/+=]{40}
    (?:;token=[A-Za-z0-9/+=]+)?$
""", re.VERBOSE)


class CopyCommand(Executable, ClauseElement):
    """
    Prepares a Redshift COPY statement.

    Parameters
    ----------
    table : sqlalchemy.Table
        The table to copy data into
    data_location : str
        The Amazon S3 location from where to copy, or a manifest file if
        the `manifest` option is used
    access_key : str
    secret_key : str
    session_token : str, optional
    delimiter : File delimiter, optional
        defaults to ','
    ignore_header : int, optional
        Integer value of number of lines to skip at the start of each file
    dangerous_null_delimiter : str, optional
        Optional string value denoting what to interpret as a NULL value from
        the file. Note that this parameter *is not properly quoted* due to a
        difference between redshift's and postgres's COPY commands
        interpretation of strings. For example, null bytes must be passed to
        redshift's ``NULL`` verbatim as ``'\\0'`` whereas postgres's ``NULL``
        accepts ``'\\x00'``.
    manifest : bool, optional
        Boolean value denoting whether data_location is a manifest file.
    empty_as_null : bool, optional
        Boolean value denoting whether to load VARCHAR fields with empty
        values as NULL instead of empty string
    blanks_as_null : bool, optional
        Boolean value denoting whether to load VARCHAR fields with whitespace
        only values as NULL instead of whitespace
    format : str, optional
        CSV, JSON, or AVRO. Indicates the type of file to copy from.
    compression : str, optional
        GZIP, LZOP, indicates the type of compression of the file to copy
    """
    formats = ['CSV', 'JSON', 'AVRO']
    compression_types = ['GZIP', 'LZOP']

    def __init__(self, table, data_location, access_key_id, secret_access_key,
                 session_token=None, delimiter=',', ignore_header=0,
                 dangerous_null_delimiter=None, manifest=False,
                 empty_as_null=True,
                 blanks_as_null=True, format='CSV', compression=None):

        credentials = 'aws_access_key_id={0};aws_secret_access_key={1}'.format(
            access_key_id,
            secret_access_key
        )

        if session_token is not None:
            credentials += ';token={0}'.format(session_token)

        if not creds_rx.match(credentials):
            raise ValueError('credentials must match the following'
                             ' format:\n'
                             'aws_access_key_id=<access-key-id>;'
                             'aws_secret_access_key=<secret-access-key>'
                             '[;token=<temporary-session-token>]\ngot %r' %
                             credentials)

        if len(delimiter) != 1:
            raise ValueError('"delimiter" parameter must be a single '
                             'character')

        if not isinstance(ignore_header, numbers.Integral):
            raise TypeError('"ignore_header" parameter should be an integer')

        if format not in self.formats:
            raise ValueError('"format" parameter must be one of %s' %
                             self.formats)

        if compression is not None and compression not in self.compression_types:
            raise ValueError('"compression" parameter must be one of %s' %
                             self.compression_types)

        self.table = table
        self.data_location = data_location
        self.credentials = credentials
        self.delimiter = delimiter
        self.ignore_header = ignore_header
        self.dangerous_null_delimiter = dangerous_null_delimiter
        self.manifest = manifest
        self.empty_as_null = empty_as_null
        self.blanks_as_null = blanks_as_null
        self.format = format
        self.compression = compression or ''


def _tablename(t, compiler):
    name = compiler.preparer.quote(t.name)
    if t.schema is not None:
        return '%s.%s' % (compiler.preparer.quote_schema(t.schema), name)
    else:
        return name


@compiles(CopyCommand)
def visit_copy_command(element, compiler, **kw):
    ''' Returns the actual sql query for the CopyCommand class
    '''
    qs = """COPY {table} FROM :data_location
    CREDENTIALS :credentials
    {format}
    TRUNCATECOLUMNS
    DELIMITER :delimiter
    IGNOREHEADER :ignore_header
    {null}
    {manifest}
    {compression}
    {empty_as_null}
    {blanks_as_null}
    """.format(table=_tablename(element.table, compiler),
               format=element.format,
               manifest='MANIFEST' if element.manifest else '',
               compression=element.compression,
               empty_as_null='EMPTYASNULL' if element.empty_as_null else '',
               blanks_as_null='BLANKSASNULL' if element.blanks_as_null else '',
               ignore_header=element.ignore_header,
               null=(("NULL '%s'" % element.dangerous_null_delimiter)
                     if element.dangerous_null_delimiter is not None else ''))

    return compiler.process(
        sa.text(qs).bindparams(
            sa.bindparam('data_location',
                         value=element.data_location,
                         type_=sa.String),
            sa.bindparam('credentials', value=element.credentials,
                         type_=sa.String),
            sa.bindparam('delimiter',
                         value=element.delimiter,
                         type_=sa.String),
            sa.bindparam('ignore_header',
                         value=element.ignore_header,
                         type_=sa.Integer)
        ),
        **kw
    )


@compiles(BindParameter)
def visit_bindparam(bindparam, compiler, **kw):
    res = compiler.visit_bindparam(bindparam, **kw)
    if 'unload_select' in kw:
        # process param and return
        res = res.replace("'", "\\'")
        res = res.replace('%', '%%')
        return res
    else:
        return res
