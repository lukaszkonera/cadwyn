import functools
import inspect
import sys
import typing
import warnings
from collections import defaultdict
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import GenericAlias, MappingProxyType, ModuleType
from typing import (
    Any,
    TypeAlias,
    TypeVar,
    _BaseGenericAlias,  # pyright: ignore[reportGeneralTypeIssues]
    cast,
    final,
    get_args,
    get_origin,
)

import fastapi.routing
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import (
    get_body_field,
    get_dependant,
    get_parameterless_sub_dependant,
)
from fastapi.params import Depends
from fastapi.routing import APIRoute
from pydantic import BaseModel
from pydantic.fields import ModelField
from starlette._utils import is_async_callable
from starlette.routing import (
    BaseRoute,
    request_response,
)
from typing_extensions import assert_never

from cadwyn._utils import Sentinel, UnionType, get_another_version_of_module
from cadwyn.codegen import _get_package_path_from_module, _get_version_dir_path
from cadwyn.exceptions import CadwynError, ModuleIsNotVersionedError, RouteAlreadyExistsError, RouterGenerationError
from cadwyn.structure import Version, VersionBundle
from cadwyn.structure.common import Endpoint, VersionDate
from cadwyn.structure.data import _SCHEMA_TO_INTERNAL_REQUEST_BODY_REPRESENTATION_MAPPING
from cadwyn.structure.endpoints import (
    EndpointDidntExistInstruction,
    EndpointExistedInstruction,
    EndpointHadInstruction,
)
from cadwyn.structure.versions import _CADWYN_REQUEST_PARAM_NAME, _CADWYN_RESPONSE_PARAM_NAME, VersionChange

_T = TypeVar("_T", bound=Callable[..., Any])
_R = TypeVar("_R", bound=fastapi.routing.APIRouter)
# This is a hack we do because we can't guarantee how the user will use the router.
_DELETED_ROUTE_TAG = "_CADWYN_DELETED_ROUTE"
EndpointPath: TypeAlias = str
EndpointMethod: TypeAlias = str


@dataclass(slots=True, frozen=True, eq=True)
class _EndpointInfo:
    endpoint_path: str
    endpoint_methods: frozenset[str]


@dataclass(slots=True)
class _RouterInfo:
    router: fastapi.routing.APIRouter
    routes_with_migrated_requests: dict[EndpointPath, set[EndpointMethod]]
    route_bodies_with_migrated_requests: set[type[BaseModel]]


def generate_versioned_routers(
    router: _R,
    versions: VersionBundle,
    latest_schemas_module: ModuleType,
) -> dict[VersionDate, _R]:
    return _EndpointTransformer(router, versions, latest_schemas_module).transform()


class VersionedAPIRouter(fastapi.routing.APIRouter):
    def only_exists_in_older_versions(self, endpoint: _T) -> _T:
        route = _get_route_from_func(self.routes, endpoint)
        if route is None:
            raise LookupError(
                f'Route not found on endpoint: "{endpoint.__name__}". '
                "Are you sure it's a route and decorators are in the correct order?",
            )
        if _DELETED_ROUTE_TAG in route.tags:
            raise CadwynError(f'The route "{endpoint.__name__}" was already deleted. You can\'t delete it again.')
        route.tags.append(_DELETED_ROUTE_TAG)
        return endpoint


@final
class _EndpointTransformer:
    def __init__(
        self,
        parent_router: fastapi.routing.APIRouter,
        versions: VersionBundle,
        latest_schemas_module: ModuleType,
    ) -> None:
        self.parent_router = parent_router
        self.versions = versions
        self.annotation_transformer = _AnnotationTransformer(latest_schemas_module, versions)

        self.routes_that_never_existed = [
            route for route in parent_router.routes if isinstance(route, APIRoute) and _DELETED_ROUTE_TAG in route.tags
        ]

    def transform(self):
        router = deepcopy(self.parent_router)
        router_infos: dict[VersionDate, _RouterInfo] = {}
        routes_with_migrated_requests = {}
        route_bodies_with_migrated_requests: set[type[BaseModel]] = set()
        for version in self.versions:
            #
            self.annotation_transformer.migrate_router_to_version(router, version)

            router_infos[version.value] = _RouterInfo(
                router,
                routes_with_migrated_requests,
                route_bodies_with_migrated_requests,
            )
            # Applying changes for the next version
            routes_with_migrated_requests = _get_migrated_routes_by_path(version)
            route_bodies_with_migrated_requests = {
                schema for change in version.version_changes for schema in change.alter_request_by_schema_instructions
            }
            router = deepcopy(router)
            self._apply_endpoint_changes_to_router(router, version)

        if self.routes_that_never_existed:
            raise RouterGenerationError(
                "Every route you mark with "
                f"@VersionedAPIRouter.{VersionedAPIRouter.only_exists_in_older_versions.__name__} "
                "must be restored in one of the older versions. Otherwise you just need to delete it altogether. "
                "The following routes have been marked with that decorator but were never restored: "
                f"{self.routes_that_never_existed}",
            )

        # BEWARE: We assume that the order of routes didn't change.
        # TODO: Make a test suite checking that it doesn't change
        for route_index, latest_route in enumerate(self.parent_router.routes):
            if not isinstance(latest_route, APIRoute):
                continue
            _add_request_and_response_params(latest_route)
            copy_of_dependant = deepcopy(latest_route.dependant)
            # Remember this: if len(body_params) == 1, then route.body_schema == route.dependant.body_params[0]
            if len(copy_of_dependant.body_params) == 1:
                body_param: ModelField = cast(ModelField, copy_of_dependant.body_params[0])
                body_schema = body_param.type_
                # TODO: Verify that this doesn't break at pydantic 2
                new_type = _SCHEMA_TO_INTERNAL_REQUEST_BODY_REPRESENTATION_MAPPING.get(body_schema, body_schema)
                new_body_param = ModelField(
                    name=body_param.name,
                    type_=new_type,
                    class_validators=body_param.class_validators,
                    model_config=body_param.model_config,
                    default=body_param.default,
                    default_factory=body_param.default_factory,
                    required=body_param.required,
                    final=body_param.final,
                    alias=body_param.alias if body_param.has_alias else None,
                    field_info=body_param.field_info,
                )
                copy_of_dependant.body_params = [new_body_param]  # pyright: ignore[reportGeneralTypeIssues]

            for older_router_info in list(router_infos.values()):
                older_route = older_router_info.router.routes[route_index]

                # We know they are APIRoutes because of the check at the very beginning of the top loop.
                # I.e. Because latest_route is an APIRoute, both routes are  APIRoutes too
                older_route = cast(APIRoute, older_route)
                # Wait.. Why do we need this code again?
                if older_route.body_field is not None and len(older_route.dependant.body_params) == 1:
                    template_older_body_model = self.annotation_transformer._change_version_of_annotations(
                        older_route.body_field.type_,
                        self.annotation_transformer.template_version_dir,
                    )
                else:
                    template_older_body_model = None
                _add_data_migrations_to_route(
                    older_route,
                    template_older_body_model,
                    older_route.body_field.alias if older_route.body_field is not None else None,
                    copy_of_dependant,
                    # NOTE: The fact that we use latest here assumes that the route can never change its response schema
                    latest_route.response_model,
                    self.versions,
                )
        for _, router_info in router_infos.items():
            router_info.router.routes = [
                route
                for route in router_info.router.routes
                if not (isinstance(route, fastapi.routing.APIRoute) and _DELETED_ROUTE_TAG in route.tags)
            ]
        return {version: router_info.router for version, router_info in router_infos.items()}

    # TODO: Simplify https://github.com/Ovsyanka83/cadwyn/issues/28
    def _apply_endpoint_changes_to_router(  # noqa: C901
        self,
        router: fastapi.routing.APIRouter,
        version: Version,
    ):
        routes = router.routes
        for version_change in version.version_changes:
            for instruction in version_change.alter_endpoint_instructions:
                original_routes = _get_routes(
                    routes,
                    instruction.endpoint_path,
                    instruction.endpoint_methods,
                    instruction.endpoint_func_name,
                    is_deleted=False,
                )
                methods_to_which_we_applied_changes = set()
                methods_we_should_have_applied_changes_to = instruction.endpoint_methods.copy()

                if isinstance(instruction, EndpointDidntExistInstruction):
                    # TODO OPTIMIZATION:
                    deleted_routes = _get_routes(
                        routes,
                        instruction.endpoint_path,
                        instruction.endpoint_methods,
                        instruction.endpoint_func_name,
                        is_deleted=True,
                    )
                    if deleted_routes:
                        method_union = set()
                        for deleted_route in deleted_routes:
                            method_union |= deleted_route.methods
                        raise RouterGenerationError(
                            f'Endpoint "{list(method_union)} {instruction.endpoint_path}" you tried to delete in '
                            f'"{version_change.__name__}" was already deleted in a newer version. If you really have '
                            f'two routes with the same paths and methods, please, use "endpoint(..., func_name=...)" '
                            f"to distinguish between them. Function names of endpoints that were already deleted: "
                            f"{[r.endpoint.__name__ for r in deleted_routes]}",
                        )
                    for original_route in original_routes:
                        methods_to_which_we_applied_changes |= original_route.methods
                        original_route.tags.append(_DELETED_ROUTE_TAG)
                    err = (
                        'Endpoint "{endpoint_methods} {endpoint_path}" you tried to delete in'
                        ' "{version_change_name}" doesn\'t exist in a newer version'
                    )
                elif isinstance(instruction, EndpointExistedInstruction):
                    # TODO Optimization
                    if original_routes:
                        method_union = set()
                        for original_route in original_routes:
                            method_union |= original_route.methods
                        raise RouterGenerationError(
                            f'Endpoint "{list(method_union)} {instruction.endpoint_path}" you tried to restore in'
                            f' "{version_change.__name__}" already existed in a newer version. If you really have two '
                            f'routes with the same paths and methods, please, use "endpoint(..., func_name=...)" to '
                            f"distinguish between them. Function names of endpoints that already existed: "
                            f"{[r.endpoint.__name__ for r in original_routes]}",
                        )
                    deleted_routes = _get_routes(
                        routes,
                        instruction.endpoint_path,
                        instruction.endpoint_methods,
                        instruction.endpoint_func_name,
                        is_deleted=True,
                    )
                    try:
                        _validate_no_repetitions_in_routes(deleted_routes)
                    except RouteAlreadyExistsError as e:
                        raise RouterGenerationError(
                            f'Endpoint "{list(instruction.endpoint_methods)} {instruction.endpoint_path}" you tried to '
                            f'restore in "{version_change.__name__}" has {len(e.routes)} applicable routes that could '
                            f"be restored. If you really have two routes with the same paths and methods, please, use "
                            f'"endpoint(..., func_name=...)" to distinguish between them. Function names of '
                            f"endpoints that can be restored: {[r.endpoint.__name__ for r in e.routes]}",
                        ) from e
                    for deleted_route in deleted_routes:
                        methods_to_which_we_applied_changes |= deleted_route.methods
                        deleted_route.tags.remove(_DELETED_ROUTE_TAG)

                        routes_that_never_existed = _get_routes(
                            self.routes_that_never_existed,
                            deleted_route.path,
                            deleted_route.methods,
                            deleted_route.endpoint.__name__,
                            is_deleted=True,
                        )
                        if len(routes_that_never_existed) == 1:
                            self.routes_that_never_existed.remove(routes_that_never_existed[0])
                        elif len(routes_that_never_existed) > 1:  # pragma: no cover
                            # I am not sure if it's possible to get to this error but I also don't want
                            # to remove it because I like its clarity very much
                            routes = routes_that_never_existed
                            raise RouterGenerationError(
                                f'Endpoint "{list(deleted_route.methods)} {deleted_route.path}" you tried to restore '
                                f'in "{version_change.__name__}" has {len(routes_that_never_existed)} applicable '
                                f"routes with the same function name and path that could be restored. This can cause "
                                f"problems during version generation. Specifically, Cadwyn won't be able to warn "
                                f"you when you deleted a route and never restored it. Please, make sure that "
                                f"functions for all these routes have different names: "
                                f"{[f'{r.endpoint.__module__}.{r.endpoint.__name__}' for r in routes]}",
                            )
                    err = (
                        'Endpoint "{endpoint_methods} {endpoint_path}" you tried to restore in'
                        ' "{version_change_name}" wasn\'t among the deleted routes'
                    )
                elif isinstance(instruction, EndpointHadInstruction):
                    for original_route in original_routes:
                        methods_to_which_we_applied_changes |= original_route.methods
                        _apply_endpoint_had_instruction(version_change, instruction, original_route)
                    err = (
                        'Endpoint "{endpoint_methods} {endpoint_path}" you tried to change in'
                        ' "{version_change_name}" doesn\'t exist'
                    )
                else:
                    assert_never(instruction)
                method_diff = methods_we_should_have_applied_changes_to - methods_to_which_we_applied_changes
                if method_diff:
                    raise RouterGenerationError(
                        err.format(
                            endpoint_methods=list(method_diff),
                            endpoint_path=instruction.endpoint_path,
                            version_change_name=version_change.__name__,
                        ),
                    )


def _validate_no_repetitions_in_routes(routes: list[fastapi.routing.APIRoute]):
    route_map = {}

    for route in routes:
        route_info = _EndpointInfo(route.path, frozenset(route.methods))
        if route_info in route_map:
            raise RouteAlreadyExistsError(route, route_map[route_info])
        route_map[route_info] = route


@final
class _AnnotationTransformer:
    __slots__ = (
        "latest_schemas_module",
        "version_dirs",
        "template_version_dir",
        "latest_version_dir",
        "change_versions_of_a_non_container_annotation",
    )

    def __init__(self, latest_schemas_module: ModuleType, versions: VersionBundle) -> None:
        if not hasattr(latest_schemas_module, "__path__"):
            raise RouterGenerationError(
                f'The latest schemas module must be a package. "{latest_schemas_module.__name__}" is not a package.',
            )
        if not latest_schemas_module.__name__.endswith(".latest"):
            raise RouterGenerationError(
                'The name of the latest schemas module must be "latest". '
                f'Received "{latest_schemas_module.__name__}" instead.',
            )
        self.latest_schemas_module = latest_schemas_module
        self.version_dirs = frozenset(
            [_get_package_path_from_module(latest_schemas_module)]
            + [_get_version_dir_path(latest_schemas_module, version.value) for version in versions],
        )
        # Okay, the naming is confusing, I know. Essentially template_version_dir is a dir of
        # latest_schemas_module while latest_version_dir is a version equivalent to latest but
        # with its own directory. Pick a better naming and make a PR, I am at your mercy.
        self.template_version_dir = min(self.version_dirs)  # "latest" < "v0000_00_00"
        self.latest_version_dir = max(self.version_dirs)  # "v2005_11_11" > "v2000_11_11"

        # This cache is not here for speeding things up. It's for preventing the creation of copies of the same object
        # because such copies could produce weird behaviors at runtime, especially if you/fastapi do any comparisons.
        # It's defined here and not on the method because of this: https://youtu.be/sVjtp6tGo0g
        self.change_versions_of_a_non_container_annotation = functools.cache(
            self._change_versions_of_a_non_container_annotation,
        )

    def migrate_router_to_version(self, router: fastapi.routing.APIRouter, version: Version):
        version_dir = _get_version_dir_path(self.latest_schemas_module, version.value)
        if not version_dir.is_dir():
            raise RouterGenerationError(
                f"Versioned schema directory '{version_dir}' does not exist.",
            )
        for route in router.routes:
            if not isinstance(route, fastapi.routing.APIRoute):
                continue
            self.migrate_route_to_version(route, version_dir)

    def migrate_route_to_version(
        self,
        route: fastapi.routing.APIRoute,
        version_dir: Path,
        *,
        ignore_response_model: bool = False,
    ):
        if route.response_model is not None and not ignore_response_model:
            route.response_model = self._change_version_of_annotations(route.response_model, version_dir)
        route.dependencies = self._change_version_of_annotations(route.dependencies, version_dir)
        route.endpoint = self._change_version_of_annotations(route.endpoint, version_dir)
        for callback in route.callbacks or []:
            if not isinstance(callback, APIRoute):
                continue
            self.migrate_route_to_version(callback, version_dir, ignore_response_model=ignore_response_model)
        _remake_endpoint_dependencies(route)

    def get_another_version_of_cls(self, cls_from_old_version: type[Any], new_version_dir: Path):
        # version_dir = /home/myuser/package/companies/v2021_01_01
        module_from_old_version = sys.modules[cls_from_old_version.__module__]
        try:
            module = get_another_version_of_module(module_from_old_version, new_version_dir, self.version_dirs)
        except ModuleIsNotVersionedError:
            return cls_from_old_version
        return getattr(module, cls_from_old_version.__name__)

    def _change_versions_of_a_non_container_annotation(self, annotation: Any, version_dir: Path) -> Any:
        if isinstance(annotation, _BaseGenericAlias | GenericAlias):
            return get_origin(annotation)[
                tuple(self._change_version_of_annotations(arg, version_dir) for arg in get_args(annotation))
            ]
        elif isinstance(annotation, Depends):
            return Depends(
                self._change_version_of_annotations(annotation.dependency, version_dir),
                use_cache=annotation.use_cache,
            )
        elif isinstance(annotation, UnionType):
            getitem = typing.Union.__getitem__  # pyright: ignore[reportGeneralTypeIssues]
            return getitem(
                tuple(self._change_version_of_annotations(a, version_dir) for a in get_args(annotation)),
            )
        elif annotation is typing.Any or isinstance(annotation, typing.NewType):
            return annotation
        elif isinstance(annotation, type):
            return self._change_version_of_type(annotation, version_dir)
        elif callable(annotation):
            # TASK: https://github.com/Ovsyanka83/cadwyn/issues/48
            if inspect.iscoroutinefunction(annotation):

                @functools.wraps(annotation)
                async def new_callable(  # pyright: ignore[reportGeneralTypeIssues]
                    *args: Any,
                    **kwargs: Any,
                ) -> Any:
                    return await annotation(*args, **kwargs)

            else:

                @functools.wraps(annotation)
                def new_callable(  # pyright: ignore[reportGeneralTypeIssues]
                    *args: Any,
                    **kwargs: Any,
                ) -> Any:
                    return annotation(*args, **kwargs)

            # Otherwise it will have the same signature as __wrapped__
            new_callable.__alt_wrapped__ = new_callable.__wrapped__  # pyright: ignore[reportGeneralTypeIssues]
            del new_callable.__wrapped__
            old_params = inspect.signature(annotation).parameters
            callable_annotations = new_callable.__annotations__

            new_callable: Any = cast(Any, new_callable)
            new_callable.__annotations__ = self._change_version_of_annotations(
                callable_annotations,
                version_dir,
            )
            new_callable.__defaults__ = self._change_version_of_annotations(
                tuple(p.default for p in old_params.values() if p.default is not inspect.Signature.empty),
                version_dir,
            )
            new_callable.__signature__ = _generate_signature(new_callable, old_params)
            return new_callable
        else:
            return annotation

    def _change_version_of_annotations(self, annotation: Any, version_dir: Path) -> Any:
        """Recursively go through all annotations and if they were taken from any versioned package, change them to the
        annotations corresponding to the version_dir passed.

        So if we had a annotation "UserResponse" from "latest" version, and we passed version_dir of "v1_0_1", it would
        replace "UserResponse" with the the same class but from the "v1_0_1" version.

        """
        if isinstance(annotation, dict):
            return {
                self._change_version_of_annotations(key, version_dir): self._change_version_of_annotations(
                    value,
                    version_dir,
                )
                for key, value in annotation.items()
            }

        elif isinstance(annotation, list | tuple):
            return type(annotation)(self._change_version_of_annotations(v, version_dir) for v in annotation)
        else:
            return self.change_versions_of_a_non_container_annotation(annotation, version_dir)

    def _change_version_of_type(self, annotation: type, version_dir: Path):
        if issubclass(annotation, BaseModel | Enum):
            if version_dir == self.latest_version_dir:
                source_file = inspect.getsourcefile(annotation)
                if source_file is None:  # pragma: no cover # I am not even sure how to cover this
                    warnings.warn(
                        f'Failed to find where the type annotation "{annotation}" is located.'
                        "Please, double check that it's located in the right directory",
                        stacklevel=7,
                    )
                else:
                    self._validate_source_file_is_located_in_template_dir(annotation, source_file)
            return self.get_another_version_of_cls(annotation, version_dir)
        else:
            return annotation

    def _validate_source_file_is_located_in_template_dir(self, annotation: type, source_file: str):
        template_dir = str(self.template_version_dir)
        dir_with_versions = str(self.template_version_dir.parent)
        # So if it is somewhere close to version dirs (either within them or next to them),
        # but not located in "latest",
        # but also not located in any other version dir
        if (
            source_file.startswith(dir_with_versions)
            and not source_file.startswith(template_dir)
            and any(source_file.startswith(str(d)) for d in self.version_dirs)
        ):
            raise RouterGenerationError(
                f'"{annotation}" is not defined in "{self.template_version_dir}" even though it must be. '
                f'It is defined in "{Path(source_file).parent}". '
                "It probably means that you used a specific version of the class in fastapi dependencies "
                'or pydantic schemas instead of "latest".',
            )


def _remake_endpoint_dependencies(route: fastapi.routing.APIRoute):
    route.dependant = get_dependant(path=route.path_format, call=route.endpoint)
    _add_request_and_response_params(route)
    route.body_field = get_body_field(dependant=route.dependant, name=route.unique_id)
    for depends in route.dependencies[::-1]:
        route.dependant.dependencies.insert(
            0,
            get_parameterless_sub_dependant(depends=depends, path=route.path_format),
        )
    route.app = request_response(route.get_route_handler())


def _add_request_and_response_params(route: APIRoute):
    if not route.dependant.request_param_name:
        route.dependant.request_param_name = _CADWYN_REQUEST_PARAM_NAME
    if not route.dependant.response_param_name:
        route.dependant.response_param_name = _CADWYN_RESPONSE_PARAM_NAME


def _add_data_migrations_to_route(
    route: APIRoute,
    template_body_field: type[BaseModel] | None,
    template_body_field_name: str | None,
    dependant_for_request_migrations: Dependant,
    latest_response_model: Any,
    versions: VersionBundle,
):
    if not is_async_callable(route.endpoint):
        raise RouterGenerationError(
            f'All versioned endpoints must be asynchronous. Endpoint "{route.endpoint}" is not.',
        )
    if not (route.dependant.request_param_name and route.dependant.response_param_name):  # pragma: no cover
        raise CadwynError(
            f"{route.dependant.request_param_name=}, {route.dependant.response_param_name=} "
            f"for route {list(route.methods)} {route.path} which should not be possible. Please, contact my author.",
        )
    route.endpoint = versions._versioned(
        template_body_field,
        template_body_field_name,
        route.dependant.body_params,
        dependant_for_request_migrations,
        latest_response_model,
        request_param_name=route.dependant.request_param_name,
        response_param_name=route.dependant.response_param_name,
    )(route.endpoint)


def _apply_endpoint_had_instruction(
    version_change: type[VersionChange],
    instruction: EndpointHadInstruction,
    original_route: APIRoute,
):
    for attr_name in instruction.attributes.__dataclass_fields__:
        attr = getattr(instruction.attributes, attr_name)
        if attr is not Sentinel:
            if getattr(original_route, attr_name) == attr:
                raise RouterGenerationError(
                    f'Expected attribute "{attr_name}" of endpoint'
                    f' "{list(original_route.methods)} {original_route.path}"'
                    f' to be different in "{version_change.__name__}", but it was the same.'
                    " It means that your version change has no effect on the attribute"
                    " and can be removed.",
                )
            setattr(original_route, attr_name, attr)


def _generate_signature(
    new_callable: Callable,
    old_params: MappingProxyType[str, inspect.Parameter],
):
    parameters = []
    default_counter = 0
    for param in old_params.values():
        if param.default is not inspect.Signature.empty:
            default = new_callable.__defaults__[default_counter]
            default_counter += 1
        else:
            default = inspect.Signature.empty
        parameters.append(
            inspect.Parameter(
                param.name,
                param.kind,
                default=default,
                annotation=new_callable.__annotations__.get(
                    param.name,
                    inspect.Signature.empty,
                ),
            ),
        )
    return inspect.Signature(
        parameters=parameters,
        return_annotation=new_callable.__annotations__.get(
            "return",
            inspect.Signature.empty,
        ),
    )


def _get_routes(
    routes: Sequence[BaseRoute],
    endpoint_path: str,
    endpoint_methods: set[str],
    endpoint_func_name: str | None = None,
    *,
    is_deleted: bool = False,
) -> list[fastapi.routing.APIRoute]:
    found_routes = []
    for route in routes:
        if (
            isinstance(route, fastapi.routing.APIRoute)
            and route.path == endpoint_path
            and set(route.methods).issubset(endpoint_methods)
            and (endpoint_func_name is None or route.endpoint.__name__ == endpoint_func_name)
            and (_DELETED_ROUTE_TAG in route.tags) == is_deleted
        ):
            found_routes.append(route)
    return found_routes


def _get_route_from_func(
    routes: Sequence[BaseRoute],
    endpoint: Endpoint,
) -> fastapi.routing.APIRoute | None:
    for route in routes:
        if isinstance(route, fastapi.routing.APIRoute) and (route.endpoint == endpoint):
            return route
    return None


def _get_migrated_routes_by_path(version: Version) -> dict[EndpointPath, set[EndpointMethod]]:
    request_by_path_migration_instructions = [
        version_change.alter_request_by_path_instructions for version_change in version.version_changes
    ]
    migrated_routes = defaultdict(set)
    for instruction_dict in request_by_path_migration_instructions:
        for path, instruction_list in instruction_dict.items():
            for instruction in instruction_list:
                migrated_routes[path] |= instruction.methods
    return migrated_routes