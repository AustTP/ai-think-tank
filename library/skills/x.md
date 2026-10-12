## Premise check

The question assumes `example.com` is the right placeholder. The better question is whether the name has to resolve at all.

If the name is only text in docs, tutorials, or sample config, any reserved name works. If something will connect to the host, `example.com` is the wrong choice. Its own page says it is not a service and should not be used for testing or monitoring.

## Purpose

Decide first whether the placeholder must resolve.

- If it is only text, use a reserved domain.
- If a tool, test, or config will connect to it, use a host you control or a documented test endpoint.
- Do not point live checks at `example.com`.

## Key facts

Supported by the collected page text (fetched in this pass and the previous one; the wording matches):

- The page is titled "Example Domain."
- It says the domain is for use in documentation examples without needing permission.
- It says: "This is not a service; avoid relying on it for testing and monitoring purposes."
- The collected text has no date or revision marker, so we cannot say how current it is.

Our inference, not checked:

- The name is one of the reserved documentation names (RFC 2606). No pass has fetched the RFC. Treat this as background only.

## Sources

- https://example.com (collected in this pass and the previous one. Both collections are the same page, so they count as one source.)

## Desk passes

**Pass 1 (announcements