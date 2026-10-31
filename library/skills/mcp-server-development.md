# MCP server development

Source: anthropics/skills @ ca45fc4 (Apache-2.0), skills/mcp-builder; adapted to house format 2026-10-31.

## Purpose
Reach for this whenever a card asks you to build or substantially change an MCP server --
the protocol this tank uses to give models real tools (its own tool surface and external
integrations alike). It exists because MCP quality is measured by whether an agent can
actually complete real tasks with the tools, not by how many endpoints got wrapped.

## Key facts
- Four phases: (1) deep research and planning, (2) implementation, (3) review with an LLM
  evaluator against real use cases, (4) testing and iteration. Do not skip planning to
  code; the tool design IS the product.
- Tool design principles:
  - Prefer comprehensive API coverage, plus workflow tools where they remove obvious
    friction; both have value.
  - Name tools consistently and action-first (prefix by service, verb-led actions), so an
    agent can find the right tool from the name alone.
  - Tool descriptions are agent-facing documentation: concise, concrete, with input/output
    shapes. Return focused, paginated/filtered data -- huge unfiltered blobs waste the
    agent's context.
  - Error messages must be actionable: say what failed, why, and the next step.
- Recommended stack per upstream guidance: TypeScript with the official SDK for new
  servers; streamable HTTP (stateless) for remote, stdio for local.
- The MCP spec lives at modelcontextprotocol.io; fetch the sitemap first, then the specific
  pages (add a .md suffix for markdown).

## Policy for this think tank
- The repo already carries MCP integration points; before building a new server, check
  whether an existing integration covers the need. Integration duplication is a card smell.
- Vendored scripts and reference material from the upstream skill (reference/,
  scripts/) stay OUT of the Library unless a director approves them; fetch from source
  when needed and cite the URL in the card.
- Any new server that can reach credentials, the filesystem, or the network needs the
  tool-safety review before it serves a live agent. An MCP tool is a trust boundary, not a
  convenience function.

## Sources
- anthropics/skills skills/mcp-builder/SKILL.md @ ca45fc4 (Apache-2.0; full terms in the
  repo's LICENSE.txt), with reference/mcp_best_practices.md as the deeper guide.
