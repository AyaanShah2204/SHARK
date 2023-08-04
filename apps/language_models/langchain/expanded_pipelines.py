from __future__ import annotations
from typing import (
    Any,
    Mapping,
    Optional,
    Dict,
    List,
    Sequence,
    Tuple,
    Union,
    Protocol,
)
import inspect
import json
import warnings
from pathlib import Path
import yaml
from abc import ABC, abstractmethod
import langchain
from langchain.base_language import BaseLanguageModel
from langchain.callbacks.base import BaseCallbackManager
from langchain.chains.question_answering import stuff_prompt
from langchain.prompts.base import BasePromptTemplate
from langchain.docstore.document import Document
from langchain.callbacks.manager import (
    CallbackManager,
    CallbackManagerForChainRun,
    Callbacks,
)
from langchain.load.serializable import Serializable
from langchain.schema import RUN_KEY, BaseMemory, RunInfo
from langchain.input import get_colored_text
from langchain.load.dump import dumpd
from langchain.prompts.prompt import PromptTemplate
from langchain.schema import LLMResult, PromptValue
from pydantic import Extra, Field, root_validator, validator


def _get_verbosity() -> bool:
    return langchain.verbose


def format_document(doc: Document, prompt: BasePromptTemplate) -> str:
    """Format a document into a string based on a prompt template."""
    base_info = {"page_content": doc.page_content}
    base_info.update(doc.metadata)
    missing_metadata = set(prompt.input_variables).difference(base_info)
    if len(missing_metadata) > 0:
        required_metadata = [
            iv for iv in prompt.input_variables if iv != "page_content"
        ]
        raise ValueError(
            f"Document prompt requires documents to have metadata variables: "
            f"{required_metadata}. Received document with missing metadata: "
            f"{list(missing_metadata)}."
        )
    document_info = {k: base_info[k] for k in prompt.input_variables}
    return prompt.format(**document_info)


class Chain(Serializable, ABC):
    """Base interface that all chains should implement."""

    memory: Optional[BaseMemory] = None
    callbacks: Callbacks = Field(default=None, exclude=True)
    callback_manager: Optional[BaseCallbackManager] = Field(
        default=None, exclude=True
    )
    verbose: bool = Field(
        default_factory=_get_verbosity
    )  # Whether to print the response text
    tags: Optional[List[str]] = None

    class Config:
        """Configuration for this pydantic object."""

        arbitrary_types_allowed = True

    @property
    def _chain_type(self) -> str:
        raise NotImplementedError("Saving not supported for this chain type.")

    @root_validator()
    def raise_deprecation(cls, values: Dict) -> Dict:
        """Raise deprecation warning if callback_manager is used."""
        if values.get("callback_manager") is not None:
            warnings.warn(
                "callback_manager is deprecated. Please use callbacks instead.",
                DeprecationWarning,
            )
            values["callbacks"] = values.pop("callback_manager", None)
        return values

    @validator("verbose", pre=True, always=True)
    def set_verbose(cls, verbose: Optional[bool]) -> bool:
        """If verbose is None, set it.

        This allows users to pass in None as verbose to access the global setting.
        """
        if verbose is None:
            return _get_verbosity()
        else:
            return verbose

    @property
    @abstractmethod
    def input_keys(self) -> List[str]:
        """Input keys this chain expects."""

    @property
    @abstractmethod
    def output_keys(self) -> List[str]:
        """Output keys this chain expects."""

    def _validate_inputs(self, inputs: Dict[str, Any]) -> None:
        """Check that all inputs are present."""
        missing_keys = set(self.input_keys).difference(inputs)
        if missing_keys:
            raise ValueError(f"Missing some input keys: {missing_keys}")

    def _validate_outputs(self, outputs: Dict[str, Any]) -> None:
        missing_keys = set(self.output_keys).difference(outputs)
        if missing_keys:
            raise ValueError(f"Missing some output keys: {missing_keys}")

    @abstractmethod
    def _call(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        """Run the logic of this chain and return the output."""

    def __call__(
        self,
        inputs: Union[Dict[str, Any], Any],
        return_only_outputs: bool = False,
        callbacks: Callbacks = None,
        *,
        tags: Optional[List[str]] = None,
        include_run_info: bool = False,
    ) -> Dict[str, Any]:
        """Run the logic of this chain and add to output if desired.

        Args:
            inputs: Dictionary of inputs, or single input if chain expects
                only one param.
            return_only_outputs: boolean for whether to return only outputs in the
                response. If True, only new keys generated by this chain will be
                returned. If False, both input keys and new keys generated by this
                chain will be returned. Defaults to False.
            callbacks: Callbacks to use for this chain run. If not provided, will
                use the callbacks provided to the chain.
            include_run_info: Whether to include run info in the response. Defaults
                to False.
        """
        inputs = self.prep_inputs(inputs)
        callback_manager = CallbackManager.configure(
            callbacks, self.callbacks, self.verbose, tags, self.tags
        )
        new_arg_supported = inspect.signature(self._call).parameters.get(
            "run_manager"
        )
        run_manager = callback_manager.on_chain_start(
            dumpd(self),
            inputs,
        )
        try:
            outputs = (
                self._call(inputs, run_manager=run_manager)
                if new_arg_supported
                else self._call(inputs)
            )
        except (KeyboardInterrupt, Exception) as e:
            run_manager.on_chain_error(e)
            raise e
        run_manager.on_chain_end(outputs)
        final_outputs: Dict[str, Any] = self.prep_outputs(
            inputs, outputs, return_only_outputs
        )
        if include_run_info:
            final_outputs[RUN_KEY] = RunInfo(run_id=run_manager.run_id)
        return final_outputs

    def prep_outputs(
        self,
        inputs: Dict[str, str],
        outputs: Dict[str, str],
        return_only_outputs: bool = False,
    ) -> Dict[str, str]:
        """Validate and prep outputs."""
        self._validate_outputs(outputs)
        if self.memory is not None:
            self.memory.save_context(inputs, outputs)
        if return_only_outputs:
            return outputs
        else:
            return {**inputs, **outputs}

    def prep_inputs(
        self, inputs: Union[Dict[str, Any], Any]
    ) -> Dict[str, str]:
        """Validate and prep inputs."""
        if not isinstance(inputs, dict):
            _input_keys = set(self.input_keys)
            if self.memory is not None:
                # If there are multiple input keys, but some get set by memory so that
                # only one is not set, we can still figure out which key it is.
                _input_keys = _input_keys.difference(
                    self.memory.memory_variables
                )
            if len(_input_keys) != 1:
                raise ValueError(
                    f"A single string input was passed in, but this chain expects "
                    f"multiple inputs ({_input_keys}). When a chain expects "
                    f"multiple inputs, please call it by passing in a dictionary, "
                    "eg `chain({'foo': 1, 'bar': 2})`"
                )
            inputs = {list(_input_keys)[0]: inputs}
        if self.memory is not None:
            external_context = self.memory.load_memory_variables(inputs)
            inputs = dict(inputs, **external_context)
        self._validate_inputs(inputs)
        return inputs

    def apply(
        self, input_list: List[Dict[str, Any]], callbacks: Callbacks = None
    ) -> List[Dict[str, str]]:
        """Call the chain on all inputs in the list."""
        return [self(inputs, callbacks=callbacks) for inputs in input_list]

    def run(
        self,
        *args: Any,
        callbacks: Callbacks = None,
        tags: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> str:
        """Run the chain as text in, text out or multiple variables, text out."""
        if len(self.output_keys) != 1:
            raise ValueError(
                f"`run` not supported when there is not exactly "
                f"one output key. Got {self.output_keys}."
            )

        if args and not kwargs:
            if len(args) != 1:
                raise ValueError(
                    "`run` supports only one positional argument."
                )
            return self(args[0], callbacks=callbacks, tags=tags)[
                self.output_keys[0]
            ]

        if kwargs and not args:
            return self(kwargs, callbacks=callbacks, tags=tags)[
                self.output_keys[0]
            ]

        if not kwargs and not args:
            raise ValueError(
                "`run` supported with either positional arguments or keyword arguments,"
                " but none were provided."
            )

        raise ValueError(
            f"`run` supported with either positional arguments or keyword arguments"
            f" but not both. Got args: {args} and kwargs: {kwargs}."
        )

    def dict(self, **kwargs: Any) -> Dict:
        """Return dictionary representation of chain."""
        if self.memory is not None:
            raise ValueError("Saving of memory is not yet supported.")
        _dict = super().dict()
        _dict["_type"] = self._chain_type
        return _dict

    def save(self, file_path: Union[Path, str]) -> None:
        """Save the chain.

        Args:
            file_path: Path to file to save the chain to.

        Example:
        .. code-block:: python

            chain.save(file_path="path/chain.yaml")
        """
        # Convert file to Path object.
        if isinstance(file_path, str):
            save_path = Path(file_path)
        else:
            save_path = file_path

        directory_path = save_path.parent
        directory_path.mkdir(parents=True, exist_ok=True)

        # Fetch dictionary to save
        chain_dict = self.dict()

        if save_path.suffix == ".json":
            with open(file_path, "w") as f:
                json.dump(chain_dict, f, indent=4)
        elif save_path.suffix == ".yaml":
            with open(file_path, "w") as f:
                yaml.dump(chain_dict, f, default_flow_style=False)
        else:
            raise ValueError(f"{save_path} must be json or yaml")


class BaseCombineDocumentsChain(Chain, ABC):
    """Base interface for chains combining documents."""

    input_key: str = "input_documents"  #: :meta private:
    output_key: str = "output_text"  #: :meta private:

    @property
    def input_keys(self) -> List[str]:
        """Expect input key.

        :meta private:
        """
        return [self.input_key]

    @property
    def output_keys(self) -> List[str]:
        """Return output key.

        :meta private:
        """
        return [self.output_key]

    def prompt_length(
        self, docs: List[Document], **kwargs: Any
    ) -> Optional[int]:
        """Return the prompt length given the documents passed in.

        Returns None if the method does not depend on the prompt length.
        """
        return None

    def _call(
        self,
        inputs: Dict[str, List[Document]],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, str]:
        _run_manager = (
            run_manager or CallbackManagerForChainRun.get_noop_manager()
        )
        docs = inputs[self.input_key]
        # Other keys are assumed to be needed for LLM prediction
        other_keys = {k: v for k, v in inputs.items() if k != self.input_key}
        doc_strings = [
            format_document(doc, self.document_prompt) for doc in docs
        ]
        # Join the documents together to put them in the prompt.
        inputs = {
            k: v
            for k, v in other_keys.items()
            if k in self.llm_chain.prompt.input_variables
        }
        inputs[self.document_variable_name] = self.document_separator.join(
            doc_strings
        )

        # Call predict on the LLM.
        output, extra_return_dict = (
            self.llm_chain(inputs, callbacks=_run_manager.get_child())[
                self.llm_chain.output_key
            ],
            {},
        )

        extra_return_dict[self.output_key] = output
        return extra_return_dict


class LLMChain(Chain):
    """Chain to run queries against LLMs.

    Example:
        .. code-block:: python

            from langchain import LLMChain, OpenAI, PromptTemplate
            prompt_template = "Tell me a {adjective} joke"
            prompt = PromptTemplate(
                input_variables=["adjective"], template=prompt_template
            )
            llm = LLMChain(llm=OpenAI(), prompt=prompt)
    """

    @property
    def lc_serializable(self) -> bool:
        return True

    prompt: BasePromptTemplate
    """Prompt object to use."""
    llm: BaseLanguageModel
    output_key: str = "text"  #: :meta private:

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True

    @property
    def input_keys(self) -> List[str]:
        """Will be whatever keys the prompt expects.

        :meta private:
        """
        return self.prompt.input_variables

    @property
    def output_keys(self) -> List[str]:
        """Will always return text key.

        :meta private:
        """
        return [self.output_key]

    def _call(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, str]:
        prompts, stop = self.prep_prompts([inputs], run_manager=run_manager)
        response = self.llm.generate_prompt(
            prompts,
            stop,
            callbacks=run_manager.get_child() if run_manager else None,
        )
        return self.create_outputs(response)[0]

    def prep_prompts(
        self,
        input_list: List[Dict[str, Any]],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Tuple[List[PromptValue], Optional[List[str]]]:
        """Prepare prompts from inputs."""
        stop = None
        if "stop" in input_list[0]:
            stop = input_list[0]["stop"]
        prompts = []
        for inputs in input_list:
            selected_inputs = {
                k: inputs[k] for k in self.prompt.input_variables
            }
            prompt = self.prompt.format_prompt(**selected_inputs)
            _colored_text = get_colored_text(prompt.to_string(), "green")
            _text = "Prompt after formatting:\n" + _colored_text
            if run_manager:
                run_manager.on_text(_text, end="\n", verbose=self.verbose)
            if "stop" in inputs and inputs["stop"] != stop:
                raise ValueError(
                    "If `stop` is present in any inputs, should be present in all."
                )
            prompts.append(prompt)
        return prompts, stop

    def apply(
        self, input_list: List[Dict[str, Any]], callbacks: Callbacks = None
    ) -> List[Dict[str, str]]:
        """Utilize the LLM generate method for speed gains."""
        callback_manager = CallbackManager.configure(
            callbacks, self.callbacks, self.verbose
        )
        run_manager = callback_manager.on_chain_start(
            dumpd(self),
            {"input_list": input_list},
        )
        try:
            response = self.generate(input_list, run_manager=run_manager)
        except (KeyboardInterrupt, Exception) as e:
            run_manager.on_chain_error(e)
            raise e
        outputs = self.create_outputs(response)
        run_manager.on_chain_end({"outputs": outputs})
        return outputs

    def create_outputs(self, response: LLMResult) -> List[Dict[str, str]]:
        """Create outputs from response."""
        return [
            # Get the text of the top generated string.
            {self.output_key: generation[0].text}
            for generation in response.generations
        ]

    def predict_and_parse(
        self, callbacks: Callbacks = None, **kwargs: Any
    ) -> Union[str, List[str], Dict[str, Any]]:
        """Call predict and then parse the results."""
        result = self.predict(callbacks=callbacks, **kwargs)
        if self.prompt.output_parser is not None:
            return self.prompt.output_parser.parse(result)
        else:
            return result

    def apply_and_parse(
        self, input_list: List[Dict[str, Any]], callbacks: Callbacks = None
    ) -> Sequence[Union[str, List[str], Dict[str, str]]]:
        """Call apply and then parse the results."""
        result = self.apply(input_list, callbacks=callbacks)
        return self._parse_result(result)

    def _parse_result(
        self, result: List[Dict[str, str]]
    ) -> Sequence[Union[str, List[str], Dict[str, str]]]:
        if self.prompt.output_parser is not None:
            return [
                self.prompt.output_parser.parse(res[self.output_key])
                for res in result
            ]
        else:
            return result

    @property
    def _chain_type(self) -> str:
        return "llm_chain"

    @classmethod
    def from_string(cls, llm: BaseLanguageModel, template: str) -> LLMChain:
        """Create LLMChain from LLM and template."""
        prompt_template = PromptTemplate.from_template(template)
        return cls(llm=llm, prompt=prompt_template)


def _get_default_document_prompt() -> PromptTemplate:
    return PromptTemplate(
        input_variables=["page_content"], template="{page_content}"
    )


class StuffDocumentsChain(BaseCombineDocumentsChain):
    """Chain that combines documents by stuffing into context."""

    llm_chain: LLMChain
    """LLM wrapper to use after formatting documents."""
    document_prompt: BasePromptTemplate = Field(
        default_factory=_get_default_document_prompt
    )
    """Prompt to use to format each document."""
    document_variable_name: str
    """The variable name in the llm_chain to put the documents in.
    If only one variable in the llm_chain, this need not be provided."""
    document_separator: str = "\n\n"
    """The string with which to join the formatted documents"""

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True

    @root_validator(pre=True)
    def get_default_document_variable_name(cls, values: Dict) -> Dict:
        """Get default document variable name, if not provided."""
        llm_chain_variables = values["llm_chain"].prompt.input_variables
        if "document_variable_name" not in values:
            if len(llm_chain_variables) == 1:
                values["document_variable_name"] = llm_chain_variables[0]
            else:
                raise ValueError(
                    "document_variable_name must be provided if there are "
                    "multiple llm_chain_variables"
                )
        else:
            if values["document_variable_name"] not in llm_chain_variables:
                raise ValueError(
                    f"document_variable_name {values['document_variable_name']} was "
                    f"not found in llm_chain input_variables: {llm_chain_variables}"
                )
        return values

    def _get_inputs(self, docs: List[Document], **kwargs: Any) -> dict:
        # Format each document according to the prompt
        doc_strings = [
            format_document(doc, self.document_prompt) for doc in docs
        ]
        # Join the documents together to put them in the prompt.
        inputs = {
            k: v
            for k, v in kwargs.items()
            if k in self.llm_chain.prompt.input_variables
        }
        inputs[self.document_variable_name] = self.document_separator.join(
            doc_strings
        )
        return inputs

    def prompt_length(
        self, docs: List[Document], **kwargs: Any
    ) -> Optional[int]:
        """Get the prompt length by formatting the prompt."""
        inputs = self._get_inputs(docs, **kwargs)
        prompt = self.llm_chain.prompt.format(**inputs)
        return self.llm_chain.llm.get_num_tokens(prompt)

    @property
    def _chain_type(self) -> str:
        return "stuff_documents_chain"


class LoadingCallable(Protocol):
    """Interface for loading the combine documents chain."""

    def __call__(
        self, llm: BaseLanguageModel, **kwargs: Any
    ) -> BaseCombineDocumentsChain:
        """Callable to load the combine documents chain."""


def _load_stuff_chain(
    llm: BaseLanguageModel,
    prompt: Optional[BasePromptTemplate] = None,
    document_variable_name: str = "context",
    verbose: Optional[bool] = None,
    callback_manager: Optional[BaseCallbackManager] = None,
    callbacks: Callbacks = None,
    **kwargs: Any,
) -> StuffDocumentsChain:
    _prompt = prompt or stuff_prompt.PROMPT_SELECTOR.get_prompt(llm)
    llm_chain = LLMChain(
        llm=llm,
        prompt=_prompt,
        verbose=verbose,
        callback_manager=callback_manager,
        callbacks=callbacks,
    )
    # TODO: document prompt
    return StuffDocumentsChain(
        llm_chain=llm_chain,
        document_variable_name=document_variable_name,
        verbose=verbose,
        callback_manager=callback_manager,
        **kwargs,
    )


def load_qa_chain(
    llm: BaseLanguageModel,
    chain_type: str = "stuff",
    verbose: Optional[bool] = None,
    callback_manager: Optional[BaseCallbackManager] = None,
    **kwargs: Any,
) -> BaseCombineDocumentsChain:
    """Load question answering chain.

    Args:
        llm: Language Model to use in the chain.
        chain_type: Type of document combining chain to use. Should be one of "stuff",
            "map_reduce", "map_rerank", and "refine".
        verbose: Whether chains should be run in verbose mode or not. Note that this
            applies to all chains that make up the final chain.
        callback_manager: Callback manager to use for the chain.

    Returns:
        A chain to use for question answering.
    """
    loader_mapping: Mapping[str, LoadingCallable] = {
        "stuff": _load_stuff_chain,
    }
    if chain_type not in loader_mapping:
        raise ValueError(
            f"Got unsupported chain type: {chain_type}. "
            f"Should be one of {loader_mapping.keys()}"
        )
    return loader_mapping[chain_type](
        llm, verbose=verbose, callback_manager=callback_manager, **kwargs
    )
