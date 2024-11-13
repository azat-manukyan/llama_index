"""DeepLake vector store index.

An index that is built within DeepLake.

"""

import logging
from typing import Any, List, Optional, cast, Dict, Union, Iterable, Callable

import uuid
from llama_index.core.bridge.pydantic import PrivateAttr
from llama_index.core.schema import BaseNode, MetadataMode, TextNode
from llama_index.core.vector_stores.types import (
    BasePydanticVectorStore,
    VectorStoreQuery,
    VectorStoreQueryResult,
    MetadataFilters,
    FilterCondition,
    FilterOperator,
)
from llama_index.core.vector_stores.utils import (
    metadata_dict_to_node,
    node_to_metadata_dict,
)

try:
    import deeplake

    if deeplake.__version__.startswith("3."):
        from deeplake.core.vectorstore import VectorStore
    else:

        class VectorStore:
            def __init__(
                self,
                path: str,
                read_only: bool = False,
                token: Optional[str] = None,
                exec_option: Optional[str] = None,
                verbose: bool = False,
                runtime: Optional[Dict] = None,
                index_params: Optional[Dict[str, Union[int, str]]] = None,
                **kwargs: Any,
            ):
                if DEEPLAKE_INSTALLED is False:
                    raise ImportError(
                        "Could not import deeplake python package. "
                        "Please install it with `pip install deeplake[enterprise]`."
                    )
                self.path = path
                self.read_only = read_only
                self.token = token
                self.exec_options = exec_option
                self.verbose = verbose
                self.runtime = runtime
                self.index_params = index_params
                self.kwargs = kwargs
                if read_only:
                    self.ds = deeplake.open_read_only(self.path, self.token)
                else:
                    try:
                        self.ds = deeplake.open(self.path, self.token)
                    except deeplake.LogNotexistsError:
                        self.ds = None

            def tensors(self) -> list[str]:
                return [c.name for c in self.ds.schema.columns]

            def add(
                self,
                text: List[str],
                metadata: Optional[List[dict]],
                embedding_data: Iterable[str],
                embedding_tensor: str,
                embedding_function: Optional[Callable] = None,
                return_ids: bool = False,
                **tensors: Any,
            ) -> Optional[list[str]]:
                if embedding_function is not None:
                    embedding_data = embedding_function(text)
                if embedding_tensor is None:
                    embedding_tensor = "embedding"
                _id = (
                    tensors["id"]
                    if "id" in tensors
                    else [str(uuid.uuid1()) for _ in range(len(text))]
                )
                if self.ds is None:
                    emb_size = len(embedding_data[0])
                    self.__create_dataset(emb_size)

                self.ds.append(
                    {
                        "text": text,
                        "metadata": metadata,
                        embedding_tensor: embedding_data,
                        "id": _id,
                    }
                )
                self.ds.commit()
                if return_ids:
                    return _id
                else:
                    return None

            def search_tql(
                self, query: str, exec_options: Optional[str]
            ) -> Dict[str, Any]:
                view = self.ds.query(query)
                return self.__view_to_docs(view)

            def search(
                self,
                embedding: Union[str, List[float]],
                k: int,
                distance_metric: str,
                filter: Optional[Dict[str, Any]],
                exec_option: Optional[str],
                deep_memory: Optional[bool],
                return_tensors: Optional[List[str]],
                query: Optional[str] = None,
            ) -> Dict[str, Any]:
                if query is None and embedding is None:
                    raise ValueError(
                        "Both `embedding` and `query` were specified."
                        " Please specify either one or the other."
                    )
                if query is not None:
                    return self.search_tql(query, exec_option)

                if isinstance(embedding, str):
                    if self.embedding_function is None:
                        raise ValueError(
                            "embedding_function is required when embedding is a string"
                        )
                    embedding = self.embedding_function.embed_documents([embedding])[0]
                emb_str = ", ".join([str(e) for e in embedding])

                column_list = " * " if not return_tensors else ", ".join(return_tensors)

                metric = self.__metric_to_function(distance_metric)
                order_by = " ASC "
                if metric == "cosine_similarity":
                    order_by = " DESC "
                dp = f"(embedding, ARRAY[{emb_str}])"
                column_list += (
                    f", {self.__metric_to_function(distance_metric)}{dp} as score"
                )
                mf = self.__metric_to_function(distance_metric)
                query = f"SELECT {column_list} ORDER BY {mf}{dp} {order_by} LIMIT {k}"
                view = self.ds.query(query)
                return self.__view_to_docs(view)

            def delete(
                self, ids: List[str], filter: Dict[str, Any], delete_all: bool
            ) -> None:
                raise NotImplementedError

            def dataset(self) -> Any:
                return self.ds

            def __view_to_docs(self, view: Any) -> Dict[str, Any]:
                docs = {}
                tenors = [(c.name, str(c.dtype)) for c in view.schema.columns]
                for name, type in tenors:
                    if type == "dict":
                        docs[name] = [i.to_dict() for i in view[name][:]]
                    else:
                        try:
                            docs[name] = view[name][:].tolist()
                        except AttributeError:
                            docs[name] = view[name][:]
                return docs

            def __metric_to_function(self, metric: str) -> str:
                if (
                    metric is None
                    or metric == "cosine"
                    or metric == "cosine_similarity"
                ):
                    return "cosine_similarity"
                elif metric == "l2" or metric == "l2_norm":
                    return "l2_norm"
                else:
                    raise ValueError(
                        f"Unknown metric: {metric}, should be one of "
                        "['cosine', 'cosine_similarity', 'l2', 'l2_norm']"
                    )

            def __create_dataset(self, emb_size=None) -> None:
                if emb_size is None:
                    if self.embedding_function is None:
                        raise ValueError(
                            "embedding_function is required to create a new dataset"
                        )
                    emb_size = len(self.embedding_function.embed_documents(["test"])[0])
                self.ds = deeplake.create(self.path, self.token)
                self.ds.add_column("text", deeplake.types.Text("inverted"))
                self.ds.add_column("metadata", deeplake.types.Dict())
                self.ds.add_column("embedding", deeplake.types.Embedding(size=emb_size))
                self.ds.add_column("id", deeplake.types.Text)
                self.ds.commit()

    DEEPLAKE_INSTALLED = True
except ImportError:
    DEEPLAKE_INSTALLED = False

logger = logging.getLogger(__name__)


class DeepLakeVectorStore(BasePydanticVectorStore):
    """The DeepLake Vector Store.

    In this vector store we store the text, its embedding and
    a few pieces of its metadata in a deeplake dataset. This implementation
    allows the use of an already existing deeplake dataset if it is one that was created
    this vector store. It also supports creating a new one if the dataset doesn't
    exist or if `overwrite` is set to True.

    Examples:
        `pip install llama-index-vector-stores-deeplake`

        ```python
        from llama_index.vector_stores.deeplake import DeepLakeVectorStore

        # Create an instance of DeepLakeVectorStore
        vector_store = DeepLakeVectorStore(dataset_path=dataset_path, overwrite=True)
        ```
    """

    stores_text: bool = True
    flat_metadata: bool = True

    ingestion_batch_size: int
    num_workers: int
    token: Optional[str]
    read_only: Optional[bool]
    dataset_path: str
    vectorstore: Any =  "VectorStore"


    _embedding_dimension: int = PrivateAttr()
    _ttl_seconds: Optional[int] = PrivateAttr()
    _deeplake_db: Any = PrivateAttr()
    _deeplake_db_collection: Any = PrivateAttr()
    _id_tensor_name: str = PrivateAttr()

    def __init__(
        self,
        dataset_path: str = "llama_index",
        token: Optional[str] = None,
        read_only: Optional[bool] = False,
        ingestion_batch_size: int = 1024,
        ingestion_num_workers: int = 4,
        overwrite: bool = False,
        exec_option: Optional[str] = None,
        verbose: bool = True,
        **kwargs: Any,
    ) -> None:
        """
        Args:
            dataset_path (str): The full path for storing to the Deep Lake Vector Store. It can be:
                - a Deep Lake cloud path of the form ``hub://org_id/dataset_name``. Requires registration with Deep Lake.
                - an s3 path of the form ``s3://bucketname/path/to/dataset``. Credentials are required in either the environment or passed to the creds argument.
                - a local file system path of the form ``./path/to/dataset`` or ``~/path/to/dataset`` or ``path/to/dataset``.
                - a memory path of the form ``mem://path/to/dataset`` which doesn't save the dataset but keeps it in memory instead. Should be used only for testing as it does not persist.
                Defaults to "llama_index".
            overwrite (bool, optional): If set to True this overwrites the Vector Store if it already exists. Defaults to False.
            token (str, optional): Activeloop token, used for fetching user credentials. This is Optional, tokens are normally autogenerated. Defaults to None.
            read_only (bool, optional): Opens dataset in read-only mode if True. Defaults to False.
            ingestion_batch_size (int): During data ingestion, data is divided
                into batches. Batch size is the size of each batch. Defaults to 1024.
            ingestion_num_workers (int): number of workers to use during data ingestion.
                Defaults to 4.
            exec_option (str): Default method for search execution. It could be either ``"auto"``, ``"python"``, ``"compute_engine"`` or ``"tensor_db"``. Defaults to ``"auto"``. If None, it's set to "auto".
                - ``auto``- Selects the best execution method based on the storage location of the Vector Store. It is the default option.
                - ``python`` - Pure-python implementation that runs on the client and can be used for data stored anywhere. WARNING: using this option with big datasets is discouraged because it can lead to memory issues.
                - ``compute_engine`` - Performant C++ implementation of the Deep Lake Compute Engine that runs on the client and can be used for any data stored in or connected to Deep Lake. It cannot be used with in-memory or local datasets.
                - ``tensor_db`` - Performant and fully-hosted Managed Tensor Database that is responsible for storage and query execution. Only available for data stored in the Deep Lake Managed Database. Store datasets in this database by specifying runtime = {"tensor_db": True} during dataset creation.

        Raises:
            ImportError: Unable to import `deeplake`.
        """
        super().__init__(
            dataset_path=dataset_path,
            token=token,
            read_only=read_only,
            ingestion_batch_size=ingestion_batch_size,
            num_workers=ingestion_num_workers,
        )

        self.vectorstore = VectorStore(
            path=dataset_path,
            ingestion_batch_size=ingestion_batch_size,
            num_workers=ingestion_num_workers,
            token=token,
            read_only=read_only,
            exec_option=exec_option,
            overwrite=overwrite,
            verbose=verbose,
            **kwargs,
        )
        try:
            self._id_tensor_name = (
                "ids" if "ids" in self.vectorstore.tensors() else "id"
            )
        except AttributeError:
            self._id_tensor_name = "id"

    @property
    def client(self) -> Any:
        """Get client.

        Returns:
            Any: DeepLake vectorstore dataset.
        """
        return self.vectorstore.dataset

    def summary(self):
        self.vectorstore.summary()

    def get_nodes(
        self,
        node_ids: Optional[List[str]] = None,
        filters: Optional[MetadataFilters] = None,
    ) -> List[BaseNode]:
        """Get nodes from vector store."""
        if node_ids:
            data = self.vectorstore.search(filter={"id": node_ids})
        else:
            data = self.vectorstore.search(filter={})

        nodes = []
        for metadata in data["metadata"]:
            nodes.append(metadata_dict_to_node(metadata))

        def filter_func(doc):
            if not filters:
                return True

            found_one = False
            for f in filters.filters:
                value = doc.metadata[f.key]
                if f.operator == FilterOperator.EQ:
                    result = value == f.value
                elif f.operator == FilterOperator.GT:
                    result = value > f.value
                elif f.operator == FilterOperator.GTE:
                    result = value >= f.value
                elif f.operator == FilterOperator.LT:
                    result = value < f.value
                elif f.operator == FilterOperator.LTE:
                    result = value <= f.value
                elif f.operator == FilterOperator.NE:
                    result = value != f.value
                elif f.operator == FilterOperator.IN:
                    result = value in f.value
                elif f.operator == FilterOperator.NOT_IN:
                    result = value not in f.value
                elif f.operator == FilterOperator.TEXT_MATCH:
                    result = f.value in value
                else:
                    raise ValueError(f"Unsupported filter operator: {f.operator}")

                if result:
                    found_one = True
                    if filters.condition == FilterCondition.OR:
                        return True
                else:
                    if filters.condition == FilterCondition.AND:
                        return False

            return found_one

        if filters:
            return [x for x in nodes if filter_func(x)]
        else:
            return nodes

    def delete_nodes(
        self,
        node_ids: Optional[List[str]] = None,
        filters: Optional[MetadataFilters] = None,
        **delete_kwargs: Any,
    ) -> None:
        if filters:
            self.vectorstore.delete(
                ids=[
                    x.node_id
                    for x in self.get_nodes(node_ids=node_ids, filters=filters)
                ]
            )
        else:
            self.vectorstore.delete(ids=node_ids)

    def clear(self) -> None:
        """Clear the vector store."""
        self.vectorstore.delete(filter=lambda x: True)

    def add(self, nodes: List[BaseNode], **add_kwargs: Any) -> List[str]:
        """Add the embeddings and their nodes into DeepLake.

        Args:
            nodes (List[BaseNode]): List of nodes with embeddings
                to insert.

        Returns:
            List[str]: List of ids inserted.
        """
        embedding = []
        metadata = []
        id_ = []
        text = []

        for node in nodes:
            embedding.append(node.get_embedding())
            metadata.append(
                node_to_metadata_dict(
                    node, remove_text=False, flat_metadata=self.flat_metadata
                )
            )
            id_.append(node.node_id)
            text.append(node.get_content(metadata_mode=MetadataMode.NONE))

        kwargs = {self._id_tensor_name: id_}

        return self.vectorstore.add(
            embedding_data=embedding,
            metadata=metadata,
            embedding_tensor="embedding",
            text=text,
            return_ids=True,
            **kwargs,
        )

    def delete(self, ref_doc_id: str, **delete_kwargs: Any) -> None:
        """
        Delete nodes using with ref_doc_id.

        Args:
            ref_doc_id (str): The doc_id of the document to delete.

        """
        self.vectorstore.delete(filter={"metadata": {"doc_id": ref_doc_id}})

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        """Query index for top k most similar nodes.

        Args:
            query (VectorStoreQuery): VectorStoreQuery class input, it has
                the following attributes:
                1. query_embedding (List[float]): query embedding
                2. similarity_top_k (int): top k most similar nodes
            deep_memory (bool): Whether to use deep memory for query execution.

        Returns:
            VectorStoreQueryResult
        """
        query_embedding = cast(List[float], query.query_embedding)
        exec_option = kwargs.get("exec_option")
        deep_memory = kwargs.get("deep_memory")
        data = self.vectorstore.search(
            embedding=query_embedding,
            exec_option=exec_option,
            k=query.similarity_top_k,
            distance_metric="cosine_similarity",
            filter=query.filters,
            return_tensors=None,
            deep_memory=deep_memory,
        )

        similarities = data["score"]
        ids = data[self._id_tensor_name]
        metadatas = data["metadata"]
        nodes = []
        for metadata in metadatas:
            if "_node_type" not in metadata:
                metadata["_node_type"] = TextNode.class_name()
            nodes.append(metadata_dict_to_node(metadata))

        return VectorStoreQueryResult(nodes=nodes, similarities=similarities, ids=ids)
