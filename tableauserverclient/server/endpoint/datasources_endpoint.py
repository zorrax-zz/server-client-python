from email.message import Message
import copy
import json
import io
import os

from contextlib import closing
from pathlib import Path
from typing import Optional, TYPE_CHECKING, Union
from collections.abc import Iterable, Mapping, Sequence

from tableauserverclient.helpers.headers import fix_filename
from tableauserverclient.server.query import QuerySet

if TYPE_CHECKING:
    from tableauserverclient.server import Server
    from tableauserverclient.models import PermissionsRule
    from .schedules_endpoint import AddResponse

from tableauserverclient.server.endpoint.dqw_endpoint import _DataQualityWarningEndpoint
from tableauserverclient.server.endpoint.endpoint import QuerysetEndpoint, api, parameter_added_in
from tableauserverclient.server.endpoint.exceptions import InternalServerError, MissingRequiredFieldError
from tableauserverclient.server.endpoint.permissions_endpoint import _PermissionsEndpoint
from tableauserverclient.server.endpoint.resource_tagger import TaggingMixin

from tableauserverclient.config import ALLOWED_FILE_EXTENSIONS, BYTES_PER_MB, config
from tableauserverclient.filesys_helpers import (
    make_download_path,
    get_file_type,
    get_file_object_size,
    to_filename,
)
from tableauserverclient.helpers.logging import logger
from tableauserverclient.models import (
    ConnectionCredentials,
    ConnectionItem,
    DatasourceItem,
    JobItem,
    RevisionItem,
    PaginationItem,
)
from tableauserverclient.server import RequestFactory, RequestOptions

io_types = (io.BytesIO, io.BufferedReader)
io_types_r = (io.BytesIO, io.BufferedReader)
io_types_w = (io.BytesIO, io.BufferedWriter)

FilePath = Union[str, os.PathLike]
FileObject = Union[io.BufferedReader, io.BytesIO]
PathOrFile = Union[FilePath, FileObject]

FilePath = Union[str, os.PathLike]
FileObjectR = Union[io.BufferedReader, io.BytesIO]
FileObjectW = Union[io.BufferedWriter, io.BytesIO]
PathOrFileR = Union[FilePath, FileObjectR]
PathOrFileW = Union[FilePath, FileObjectW]


class Datasources(QuerysetEndpoint[DatasourceItem], TaggingMixin[DatasourceItem]):
    def __init__(self, parent_srv: "Server") -> None:
        super().__init__(parent_srv)
        self._permissions = _PermissionsEndpoint(parent_srv, lambda: self.baseurl)
        self._data_quality_warnings = _DataQualityWarningEndpoint(self.parent_srv, "datasource")

        return None

    @property
    def baseurl(self) -> str:
        return f"{self.parent_srv.baseurl}/sites/{self.parent_srv.site_id}/datasources"

    # Get all datasources
    @api(version="2.0")
    def get(self, req_options: Optional[RequestOptions] = None) -> tuple[list[DatasourceItem], PaginationItem]:
        logger.info("Querying all datasources on site")
        url = self.baseurl
        server_response = self.get_request(url, req_options)
        pagination_item = PaginationItem.from_response(server_response.content, self.parent_srv.namespace)
        all_datasource_items = DatasourceItem.from_response(server_response.content, self.parent_srv.namespace)
        return all_datasource_items, pagination_item

    # Get 1 datasource by id
    @api(version="2.0")
    def get_by_id(self, datasource_id: str) -> DatasourceItem:
        if not datasource_id:
            error = "Datasource ID undefined."
            raise ValueError(error)
        logger.info(f"Querying single datasource (ID: {datasource_id})")
        url = f"{self.baseurl}/{datasource_id}"
        server_response = self.get_request(url)
        return DatasourceItem.from_response(server_response.content, self.parent_srv.namespace)[0]

    # Populate datasource item's connections
    @api(version="2.0")
    def populate_connections(self, datasource_item: DatasourceItem) -> None:
        if not datasource_item.id:
            error = "Datasource item missing ID. Datasource must be retrieved from server first."
            raise MissingRequiredFieldError(error)

        def connections_fetcher():
            return self._get_datasource_connections(datasource_item)

        datasource_item._set_connections(connections_fetcher)
        logger.info(f"Populated connections for datasource (ID: {datasource_item.id})")

    def _get_datasource_connections(
        self, datasource_item: DatasourceItem, req_options: Optional[RequestOptions] = None
    ) -> list[ConnectionItem]:
        url = f"{self.baseurl}/{datasource_item.id}/connections"
        server_response = self.get_request(url, req_options)
        connections = ConnectionItem.from_response(server_response.content, self.parent_srv.namespace)
        for connection in connections:
            connection._datasource_id = datasource_item.id
            connection._datasource_name = datasource_item.name
        return connections

    # Delete 1 datasource by id
    @api(version="2.0")
    def delete(self, datasource_id: str) -> None:
        if not datasource_id:
            error = "Datasource ID undefined."
            raise ValueError(error)
        url = f"{self.baseurl}/{datasource_id}"
        self.delete_request(url)
        logger.info(f"Deleted single datasource (ID: {datasource_id})")

    # Download 1 datasource by id
    @api(version="2.0")
    @parameter_added_in(no_extract="2.5")
    @parameter_added_in(include_extract="2.5")
    def download(
        self,
        datasource_id: str,
        filepath: Optional[PathOrFileW] = None,
        include_extract: bool = True,
    ) -> PathOrFileW:
        return self.download_revision(
            datasource_id,
            None,
            filepath,
            include_extract,
        )

    # Update datasource
    @api(version="2.0")
    def update(self, datasource_item: DatasourceItem) -> DatasourceItem:
        if not datasource_item.id:
            error = "Datasource item missing ID. Datasource must be retrieved from server first."
            raise MissingRequiredFieldError(error)
        # bug - before v3.15 you must always include the project id
        if datasource_item.owner_id and not datasource_item.project_id:
            if not self.parent_srv.check_at_least_version("3.15"):
                error = (
                    "Attempting to set new owner but datasource is missing Project ID."
                    "In versions before 3.15 the project id must be included to update the owner."
                )
                raise MissingRequiredFieldError(error)

        self.update_tags(datasource_item)

        # Update the datasource itself
        url = f"{self.baseurl}/{datasource_item.id}"

        update_req = RequestFactory.Datasource.update_req(datasource_item)
        server_response = self.put_request(url, update_req)
        logger.info(f"Updated datasource item (ID: {datasource_item.id})")
        updated_datasource = copy.copy(datasource_item)
        return updated_datasource._parse_common_elements(server_response.content, self.parent_srv.namespace)

    # Update datasource connections
    @api(version="2.3")
    def update_connection(
        self, datasource_item: DatasourceItem, connection_item: ConnectionItem
    ) -> Optional[ConnectionItem]:
        url = f"{self.baseurl}/{datasource_item.id}/connections/{connection_item.id}"

        update_req = RequestFactory.Connection.update_req(connection_item)
        server_response = self.put_request(url, update_req)
        connections = ConnectionItem.from_response(server_response.content, self.parent_srv.namespace)
        if not connections:
            return None

        if len(connections) > 1:
            logger.debug(f"Multiple connections returned ({len(connections)})")
        connection = list(filter(lambda x: x.id == connection_item.id, connections))[0]

        logger.info(f"Updated datasource item (ID: {datasource_item.id} & connection item {connection_item.id}")
        return connection

    @api(version="2.8")
    def refresh(self, datasource_item: DatasourceItem, incremental: bool = False) -> JobItem:
        id_ = getattr(datasource_item, "id", datasource_item)
        url = f"{self.baseurl}/{id_}/refresh"
        # refresh_req = RequestFactory.Task.refresh_req(incremental)
        refresh_req = RequestFactory.Empty.empty_req()
        server_response = self.post_request(url, refresh_req)
        new_job = JobItem.from_response(server_response.content, self.parent_srv.namespace)[0]
        return new_job

    @api(version="3.5")
    def create_extract(self, datasource_item: DatasourceItem, encrypt: bool = False) -> JobItem:
        id_ = getattr(datasource_item, "id", datasource_item)
        url = f"{self.baseurl}/{id_}/createExtract?encrypt={encrypt}"
        empty_req = RequestFactory.Empty.empty_req()
        server_response = self.post_request(url, empty_req)
        new_job = JobItem.from_response(server_response.content, self.parent_srv.namespace)[0]
        return new_job

    @api(version="3.5")
    def delete_extract(self, datasource_item: DatasourceItem) -> None:
        id_ = getattr(datasource_item, "id", datasource_item)
        url = f"{self.baseurl}/{id_}/deleteExtract"
        empty_req = RequestFactory.Empty.empty_req()
        self.post_request(url, empty_req)

    # Publish datasource
    @api(version="2.0")
    @parameter_added_in(connections="2.8")
    @parameter_added_in(as_job="3.0")
    def publish(
        self,
        datasource_item: DatasourceItem,
        file: PathOrFileR,
        mode: str,
        connection_credentials: Optional[ConnectionCredentials] = None,
        connections: Optional[Sequence[ConnectionItem]] = None,
        as_job: bool = False,
    ) -> Union[DatasourceItem, JobItem]:
        if isinstance(file, (os.PathLike, str)):
            if not os.path.isfile(file):
                error = "File path does not lead to an existing file."
                raise OSError(error)

            filename = os.path.basename(file)
            file_extension = os.path.splitext(filename)[1][1:]
            file_size = os.path.getsize(file)
            logger.debug(f"Publishing file `{filename}`, size `{file_size}`")
            # If name is not defined, grab the name from the file to publish
            if not datasource_item.name:
                datasource_item.name = os.path.splitext(filename)[0]
            if file_extension not in ALLOWED_FILE_EXTENSIONS:
                error = "Only {} files can be published as datasources.".format(", ".join(ALLOWED_FILE_EXTENSIONS))
                raise ValueError(error)

        elif isinstance(file, io_types_r):
            if not datasource_item.name:
                error = "Datasource item must have a name when passing a file object"
                raise ValueError(error)

            file_type = get_file_type(file)
            if file_type == "zip":
                file_extension = "tdsx"
            elif file_type == "xml":
                file_extension = "tds"
            else:
                error = f"Unsupported file type {file_type}"
                raise ValueError(error)

            filename = f"{datasource_item.name}.{file_extension}"
            file_size = get_file_object_size(file)

        else:
            raise TypeError("file should be a filepath or file object.")

        # Construct the url with the defined mode
        url = f"{self.baseurl}?datasourceType={file_extension}"
        if not mode or not hasattr(self.parent_srv.PublishMode, mode):
            error = f"Invalid mode defined: {mode}"
            raise ValueError(error)
        else:
            url += f"&{mode.lower()}=true"

        if as_job:
            url += "&{}=true".format("asJob")

        # Determine if chunking is required (64MB is the limit for single upload method)
        if file_size >= config.FILESIZE_LIMIT_MB * BYTES_PER_MB:
            logger.info(
                "Publishing {} to server with chunking method (datasource over {}MB, chunk size {}MB)".format(
                    filename, config.FILESIZE_LIMIT_MB, config.CHUNK_SIZE_MB
                )
            )
            upload_session_id = self.parent_srv.fileuploads.upload(file)
            url = f"{url}&uploadSessionId={upload_session_id}"
            xml_request, content_type = RequestFactory.Datasource.publish_req_chunked(
                datasource_item, connection_credentials, connections
            )
        else:
            logger.info(f"Publishing {filename} to server")

            if isinstance(file, (Path, str)):
                with open(file, "rb") as f:
                    file_contents = f.read()
            elif isinstance(file, io_types_r):
                file_contents = file.read()
            else:
                raise TypeError("file should be a filepath or file object.")

            xml_request, content_type = RequestFactory.Datasource.publish_req(
                datasource_item,
                filename,
                file_contents,
                connection_credentials,
                connections,
            )

        # Send the publishing request to server
        try:
            server_response = self.post_request(url, xml_request, content_type)
        except InternalServerError as err:
            if err.code == 504 and not as_job:
                err.content = "Timeout error while publishing. Please use asynchronous publishing to avoid timeouts."
            raise err

        if as_job:
            new_job = JobItem.from_response(server_response.content, self.parent_srv.namespace)[0]
            logger.info(f"Published {filename} (JOB_ID: {new_job.id}")
            return new_job
        else:
            new_datasource = DatasourceItem.from_response(server_response.content, self.parent_srv.namespace)[0]
            logger.info(f"Published {filename} (ID: {new_datasource.id})")
            return new_datasource

    @api(version="3.13")
    def update_hyper_data(
        self,
        datasource_or_connection_item: Union[DatasourceItem, ConnectionItem, str],
        *,
        request_id: str,
        actions: Sequence[Mapping],
        payload: Optional[FilePath] = None,
    ) -> JobItem:
        if isinstance(datasource_or_connection_item, DatasourceItem):
            datasource_id = datasource_or_connection_item.id
            url = f"{self.baseurl}/{datasource_id}/data"
        elif isinstance(datasource_or_connection_item, ConnectionItem):
            datasource_id = datasource_or_connection_item.datasource_id
            connection_id = datasource_or_connection_item.id
            url = f"{self.baseurl}/{datasource_id}/connections/{connection_id}/data"
        else:
            assert isinstance(datasource_or_connection_item, str)
            url = f"{self.baseurl}/{datasource_or_connection_item}/data"

        if payload is not None:
            if not os.path.isfile(payload):
                error = "File path does not lead to an existing file."
                raise OSError(error)

            logger.info(f"Uploading {payload} to server with chunking method for Update job")
            upload_session_id = self.parent_srv.fileuploads.upload(payload)
            url = f"{url}?uploadSessionId={upload_session_id}"

        json_request = json.dumps({"actions": actions})
        parameters = {"headers": {"requestid": request_id}}
        server_response = self.patch_request(url, json_request, "application/json", parameters=parameters)
        new_job = JobItem.from_response(server_response.content, self.parent_srv.namespace)[0]
        return new_job

    @api(version="2.0")
    def populate_permissions(self, item: DatasourceItem) -> None:
        self._permissions.populate(item)

    @api(version="2.0")
    def update_permissions(self, item: DatasourceItem, permission_item: list["PermissionsRule"]) -> None:
        self._permissions.update(item, permission_item)

    @api(version="2.0")
    def delete_permission(self, item: DatasourceItem, capability_item: "PermissionsRule") -> None:
        self._permissions.delete(item, capability_item)

    @api(version="3.5")
    def populate_dqw(self, item):
        self._data_quality_warnings.populate(item)

    @api(version="3.5")
    def update_dqw(self, item, warning):
        return self._data_quality_warnings.update(item, warning)

    @api(version="3.5")
    def add_dqw(self, item, warning):
        return self._data_quality_warnings.add(item, warning)

    @api(version="3.5")
    def delete_dqw(self, item):
        self._data_quality_warnings.clear(item)

    # Populate datasource item's revisions
    @api(version="2.3")
    def populate_revisions(self, datasource_item: DatasourceItem) -> None:
        if not datasource_item.id:
            error = "Datasource item missing ID. Datasource must be retrieved from server first."
            raise MissingRequiredFieldError(error)

        def revisions_fetcher():
            return self._get_datasource_revisions(datasource_item)

        datasource_item._set_revisions(revisions_fetcher)
        logger.info(f"Populated revisions for datasource (ID: {datasource_item.id})")

    def _get_datasource_revisions(
        self, datasource_item: DatasourceItem, req_options: Optional["RequestOptions"] = None
    ) -> list[RevisionItem]:
        url = f"{self.baseurl}/{datasource_item.id}/revisions"
        server_response = self.get_request(url, req_options)
        revisions = RevisionItem.from_response(server_response.content, self.parent_srv.namespace, datasource_item)
        return revisions

    # Download 1 datasource revision by revision number
    @api(version="2.3")
    def download_revision(
        self,
        datasource_id: str,
        revision_number: Optional[str],
        filepath: Optional[PathOrFileW] = None,
        include_extract: bool = True,
    ) -> PathOrFileW:
        if not datasource_id:
            error = "Datasource ID undefined."
            raise ValueError(error)
        if revision_number is None:
            url = f"{self.baseurl}/{datasource_id}/content"
        else:
            url = f"{self.baseurl}/{datasource_id}/revisions/{revision_number}/content"

        if not include_extract:
            url += "?includeExtract=False"

        with closing(self.get_request(url, parameters={"stream": True})) as server_response:
            m = Message()
            m["Content-Disposition"] = server_response.headers["Content-Disposition"]
            params = m.get_filename(failobj="")
            if isinstance(filepath, io_types_w):
                for chunk in server_response.iter_content(1024):  # 1KB
                    filepath.write(chunk)
                return_path = filepath
            else:
                params = fix_filename(params)
                filename = to_filename(os.path.basename(params))
                download_path = make_download_path(filepath, filename)
                with open(download_path, "wb") as f:
                    for chunk in server_response.iter_content(1024):  # 1KB
                        f.write(chunk)
                return_path = os.path.abspath(download_path)

        logger.info(f"Downloaded datasource revision {revision_number} to {return_path} (ID: {datasource_id})")
        return return_path

    @api(version="2.3")
    def delete_revision(self, datasource_id: str, revision_number: str) -> None:
        if datasource_id is None or revision_number is None:
            raise ValueError
        url = "/".join([self.baseurl, datasource_id, "revisions", revision_number])

        self.delete_request(url)
        logger.info(f"Deleted single datasource revision (ID: {datasource_id}) (Revision: {revision_number})")

    # a convenience method
    @api(version="2.8")
    def schedule_extract_refresh(
        self, schedule_id: str, item: DatasourceItem
    ) -> list["AddResponse"]:  # actually should return a task
        return self.parent_srv.schedules.add_to_schedule(schedule_id, datasource=item)

    @api(version="1.0")
    def add_tags(self, item: Union[DatasourceItem, str], tags: Union[Iterable[str], str]) -> set[str]:
        return super().add_tags(item, tags)

    @api(version="1.0")
    def delete_tags(self, item: Union[DatasourceItem, str], tags: Union[Iterable[str], str]) -> None:
        return super().delete_tags(item, tags)

    @api(version="1.0")
    def update_tags(self, item: DatasourceItem) -> None:
        return super().update_tags(item)

    def filter(self, *invalid, page_size: Optional[int] = None, **kwargs) -> QuerySet[DatasourceItem]:
        """
        Queries the Tableau Server for items using the specified filters. Page
        size can be specified to limit the number of items returned in a single
        request. If not specified, the default page size is 100. Page size can
        be an integer between 1 and 1000.

        No positional arguments are allowed. All filters must be specified as
        keyword arguments. If you use the equality operator, you can specify it
        through <field_name>=<value>. If you want to use a different operator,
        you can specify it through <field_name>__<operator>=<value>. Field
        names can either be in snake_case or camelCase.

        This endpoint supports the following fields and operators:


        authentication_type=...
        authentication_type__in=...
        connected_workbook_type=...
        connected_workbook_type__gt=...
        connected_workbook_type__gte=...
        connected_workbook_type__lt=...
        connected_workbook_type__lte=...
        connection_to=...
        connection_to__in=...
        connection_type=...
        connection_type__in=...
        content_url=...
        content_url__in=...
        created_at=...
        created_at__gt=...
        created_at__gte=...
        created_at__lt=...
        created_at__lte=...
        database_name=...
        database_name__in=...
        database_user_name=...
        database_user_name__in=...
        description=...
        description__in=...
        favorites_total=...
        favorites_total__gt=...
        favorites_total__gte=...
        favorites_total__lt=...
        favorites_total__lte=...
        has_alert=...
        has_embedded_password=...
        has_extracts=...
        is_certified=...
        is_connectable=...
        is_default_port=...
        is_hierarchical=...
        is_published=...
        name=...
        name__in=...
        owner_domain=...
        owner_domain__in=...
        owner_email=...
        owner_name=...
        owner_name__in=...
        project_name=...
        project_name__in=...
        server_name=...
        server_name__in=...
        server_port=...
        size=...
        size__gt=...
        size__gte=...
        size__lt=...
        size__lte=...
        table_name=...
        table_name__in=...
        tags=...
        tags__in=...
        type=...
        updated_at=...
        updated_at__gt=...
        updated_at__gte=...
        updated_at__lt=...
        updated_at__lte=...
        """

        return super().filter(*invalid, page_size=page_size, **kwargs)
