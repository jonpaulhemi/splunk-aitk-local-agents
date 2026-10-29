"""The agent's skills: the invoke payload's `skills` as a prompt listing and a `load_skill` tool.

AI Toolkit resolves the skills attached to an agent to {name, description,
skill_text} before invoking. The model sees each name and description, and
reads a skill's full instructions only when it calls `load_skill`, so long
skills stay out of the context until they're needed.
"""

TOOL = "load_skill"
TRACE_TYPE = "skill"  # the trace `type` AI Toolkit's UI shows as a skill


class Skills:
    """The skills attached to one run, and the ones loaded during it."""

    def __init__(self, attached):
        self._by_name = {}
        for skill in attached or []:
            if isinstance(skill, dict) and skill.get("name"):
                self._by_name.setdefault(skill["name"], skill)
        self.loaded = []  # names, in load order, each once

    def prompt(self):
        """What the system prompt says about the skills, or None if there are none."""
        if not self._by_name:
            return None
        listing = "\n".join(f"- {name}: {skill.get('description') or ''}".rstrip()
                            for name, skill in self._by_name.items())
        return ("You have skills: written instructions for particular kinds of task. When a task matches a "
                f"skill's description, call {TOOL} with its name and follow the instructions it returns.\n\n"
                + listing)

    def tools(self):
        """[load_skill], or [] if there are no skills."""
        if not self._by_name:
            return []
        from langchain_core.tools import StructuredTool, ToolException

        def load_skill(name: str) -> str:
            skill = self._by_name.get(name)
            if skill is None:
                raise ToolException(f"There is no skill named '{name}'. Skills: {', '.join(self._by_name)}")
            if name not in self.loaded:
                self.loaded.append(name)
            return skill.get("skill_text") or ""

        return [StructuredTool.from_function(
            load_skill, name=TOOL, handle_tool_error=True,
            description="Read a skill's full instructions. `name` is one of the skills listed in your instructions.")]

    def reported(self):
        """The final chunk's `skills`: those loaded in this invocation."""
        return [{"name": name} for name in self.loaded]
