import os
import inspect
import logging
from importlib import import_module

import toml
from terminaltables import SingleTable
from validr import modelclass, Invalid, Compiler, fields, asdict
from weirb import run
from weirb import Request as HttpRequest
from weirb import Config as ServerConfig
from weirb.error import HttpError

from .error import ConfigError, DependencyError, HrpcError, InternalError
from .response import ErrorResponse
from .context import Context
from .helper import import_all_modules
from .config import InternalConfig, INTERNAL_VALIDATORS
from .service import Service
from .router import Router

LOG = logging.getLogger(__name__)


class App:
    def __init__(self, import_name, **cli_config):
        self.import_name = import_name
        self._load_config_module()
        self._load_intro()
        self._load_plugins()
        self._load_schema_compiler()
        self._load_config_class()
        self._load_config(cli_config)
        self._config_dict = asdict(self.config)
        self._active_plugins()
        self._load_services()
        self.router = Router(self.services, self.config.url_prefix)

    def __repr__(self):
        return f'<App {self.import_name}>'

    def _load_config_module(self):
        self.config_module = None
        try:
            self.config_module = import_module(f'{self.import_name}.config')
        except ImportError:
            try:
                self.config_module = import_module(self.import_name)
            except ImportError:
                pass

    def _load_intro(self):
        if hasattr(self.config_module, 'intro'):
            self.intro = self.config_module.intro or ''
        else:
            self.intro = ''

    def _load_plugins(self):
        if hasattr(self.config_module, 'plugins'):
            self.plugins = list(self.config_module.plugins)
        else:
            self.plugins = []

    def _load_schema_compiler(self):
        self.validators = INTERNAL_VALIDATORS.copy()
        if hasattr(self.config_module, 'validators'):
            self.validators.update(self.config_module.validators)
        self.schema_compiler = Compiler(self.validators)

    def _load_config_class(self):
        """
        user_config > internal_config > plugin_config
        """
        configs = []
        if hasattr(self.config_module, 'Config'):
            configs.append(self.config_module.Config)
        configs.append(InternalConfig)
        for plugin in self.plugins:
            if hasattr(plugin, 'Config'):
                configs.append(plugin.Config)
        config_class = type('Config', tuple(configs), {})
        self.config_class = modelclass(
            config_class, compiler=self.schema_compiler, immutable=True)

    def _load_config(self, cli_config):
        name = self.import_name.replace('.', '_')
        key = f'{name}_config'.upper()
        config_path = os.getenv(key, None)
        if config_path:
            print(f'* Load config file {config_path!r}')
            try:
                with open(config_path) as f:
                    content = f.read()
            except FileNotFoundError:
                msg = f'config file {config_path!r} not found'
                raise ConfigError(msg) from None
            try:
                config = toml.loads(content)
            except toml.TomlDecodeError:
                msg = f'config file {config_path!r} is not valid TOML file'
                raise ConfigError(msg) from None
            config.update(cli_config)
        else:
            print(f'* No config file provided, you can set config path'
                  f' by {key} environment variable')
            config = cli_config
        try:
            self.config = self.config_class(**config)
        except Invalid as ex:
            raise ConfigError(ex.message) from None

    def _active_plugins(self):
        self.contexts = []
        self.decorators = []
        self.provides = {f'config.{key}' for key in fields(self.config)}
        for plugin in self.plugins:
            plugin.active(self)
            if hasattr(plugin, 'context'):
                self.contexts.append(plugin.context)
            if hasattr(plugin, 'decorator'):
                self.decorators.append(plugin.decorator)
            if hasattr(plugin, 'provides'):
                self.provides.update(plugin.provides)
        for plugin in self.plugins:
            if not hasattr(plugin, 'requires'):
                continue
            requires = set(plugin.requires)
            missing = ', '.join(requires - self.provides)
            if missing:
                msg = f'the requires {missing} of plugin {plugin} is missing'
                raise DependencyError(msg)

    def _load_services(self):
        service_classes = set(_import_services(self.import_name))
        self.services = []
        for cls in service_classes:
            s = Service(
                cls, self.provides, self.decorators, self.schema_compiler)
            self.services.append(s)

    def context(self):
        return Context(self._config_dict, self.contexts, self._handler)

    async def _handler(self, context, raw_request):
        http_request = HttpRequest(raw_request)
        try:
            method = self.router.lookup(http_request.method, http_request.path)
            http_response = await method(context, http_request)
        except HrpcError as ex:
            http_response = ErrorResponse(ex).to_http()
        except HttpError:
            raise
        except Exception as ex:
            LOG.error('Error raised when handle request:', exc_info=ex)
            http_response = ErrorResponse(InternalError()).to_http()
        return http_response

    def run(self):
        if self.config.print_config:
            self.print_config()
        if self.config.print_plugin:
            self.print_plugin()
        if self.config.print_service:
            self.print_service()
        server_config = asdict(self.config, keys=fields(ServerConfig))
        run(self, **server_config)

    def print_config(self):
        table = [('Key', 'Value', 'Schema')]
        config_schema = self.config.__schema__.items
        for key, value in sorted(asdict(self.config).items()):
            schema = config_schema[key]
            table.append(
                (key, _shorten(str(value)), _shorten(schema.repr()))
            )
        table = SingleTable(table, title='Configs')
        print(table.table)

    def print_plugin(self):
        title = 'Plugins' if self.plugins else 'No plugins'
        table = [('#', 'Name', 'Provides', 'Requires', 'Contributes')]
        for idx, plugin in enumerate(self.plugins, 1):
            name = type(plugin).__name__
            contributes = []
            provides = ''
            requires = ''
            if hasattr(plugin, 'context'):
                contributes.append('context')
            if hasattr(plugin, 'decorator'):
                contributes.append('decorator')
            contributes = ', '.join(contributes)
            if hasattr(plugin, 'provides'):
                provides = ', '.join(plugin.provides)
            if hasattr(plugin, 'requires'):
                requires = ', '.join(plugin.requires)
            table.append((idx, name, provides, requires, contributes))
        table = SingleTable(table, title=title)
        print(table.table)

    def print_service(self):
        title = 'Services' if self.services else 'No services'
        table = [('#', 'Name', 'Methods', 'Requires')]
        for idx, service in enumerate(self.services, 1):
            methods = [m.name for m in service.methods]
            methods = ', '.join(methods)
            requires = [field.key for field in service.fields.values()]
            requires = ', '.join(requires)
            table.append((idx, service.name, methods, requires))
        table = SingleTable(table, title=title)
        print(table.table)


def _import_services(import_name):
    suffix = 'Service'
    for module in import_all_modules(import_name):
        for name, obj in vars(module).items():
            is_service = name != suffix and name.endswith(suffix)
            if is_service and inspect.isclass(obj):
                yield obj


def _shorten(x, w=30):
    return (x[:w] + '...') if len(x) > w else x
