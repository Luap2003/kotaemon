import asyncio
import logging
from kotaemon.base import BaseComponent, Document, HumanMessage, Node, SystemMessage
from kotaemon.llms import ChatLLM
from ktem.reasoning.base import BaseReasoning
from ktem.llms.manager import llms

logger = logging.getLogger(__name__)

class SimpleQueryComponent(BaseReasoning):
    """
    A simple query component that receives a user query, gets an answer from the LLM,
    and streams that answer back to the UI.

    How it works:
      1. The run method wraps the user question into a HumanMessage.
      2. It then passes the message to the default LLM.
      3. The full answer is split into small chunks.
      4. Each chunk is sent to the UI via self.report_output (simulated streaming).
      5. Finally, the full answer is returned as a Document of channel 'chat'.
    """

    # Define a node for the language model, using the default LLM from ktem.llms.manager
    llm: ChatLLM = Node(default_callback=lambda _: llms.get_default())
    
    def stream(self, message: str, conv_id: str, history: list, **kwargs):
        # Simple prompt with chat history
        messages = [
            SystemMessage(content="You are a helpful assistant"),
            HumanMessage(content=message),
        ]
        
        # Get streaming response from LLM
        response = self.llm.stream(messages)
        
        # Yield the response chunks
        for chunk in response:
            yield Document(
                channel="chat",
                content=chunk.text,
            )

    @classmethod
    def get_info(cls) -> dict:
        return {
            "id": "direct_llm",
            "name": "Direct LLM",
            "description": "Directly answers questions using the LLM without any retrieval",
        }
    
    @classmethod
    def get_user_settings(cls) -> dict:
        """Optional: Return settings for this component to be shown in the app UI."""
        return  {}
    
    @classmethod
    def get_user_settings(cls) -> dict:
        from ktem.llms.manager import llms
        
        llm_choices = [("(default)", "")]
        try:
            llm_choices += [(name, name) for name in llms.options().keys()]
        except Exception as e:
            logger.exception(f"Failed to get LLM options: {e}")

        return {
            "llm": {
                "name": "Language Model",
                "value": "",
                "component": "dropdown",
                "choices": llm_choices,
                "special_type": "llm",
                "info": "The language model to use for generating answers",
            },
            "system_prompt": {
                "name": "System Prompt",
                "value": "You are a helpful assistant",
                "info": "Initial instructions to give the LLM"
            }
        }