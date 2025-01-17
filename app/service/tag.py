from datetime import datetime
from typing import List
from uuid import UUID, uuid4

from app.lib.exceptions import NotFound
from app.logger import logger
from app.models.pagination import SortingArgs, FilteringArgs, PaginationArgs
from app.models.sequence import DocumentSequence
from app.service.audit import AuditService
from app.service.base import BaseService
from app.service.tag_history import TagHistoryService


def add_to_history(wrapped):
    """Decorator we use to decorate our Tag methods. So long as a tag method returns the complete JSON response from
    ES for any one given tag (the successfully updated object), this decorator will then use it and add it to the
    history Index"""

    async def inner(self, *args, **kwargs) -> tuple:
        audit_args, ret = await wrapped(self, *args, **kwargs)
        await self.history_service.add(ret)
        return audit_args, ret

    return inner


def audit(wrapped):
    """Decorator to audit all service calls. This will add an audit log entry for every call made to the service"""

    async def inner(self, *args, **kwargs) -> dict:
        audit_args, ret = await wrapped(self, *args, **kwargs)
        audit_args["user"] = self.user.username
        await self.audit_service.add(**audit_args)
        return ret

    return inner


class TagService(BaseService):

    _index_name = "tags-latest"

    def __init__(self, user, client, history_service: TagHistoryService, audit_service: AuditService):
        super().__init__(client)
        self.user = user
        self.history_service = history_service
        self.audit_service = audit_service

    def _update_meta(self, payload):
        """Update the meta information for a tag"""
        now = datetime.utcnow()
        payload["updated"] = now
        payload["editor"] = self.user.username
        payload["state"] = None
        return payload

    async def count(self, include_deleted: bool = False) -> int:
        """Counts all tags, by default excluding those that are deleted
        :arg include_deleted if True, include deleted tags in the count"""
        query = None
        if not include_deleted:
            query = {"query": {"bool": {"must_not": {"exists": {"field": "deleted"}}}}}
        return await super(TagService, self).count(query)

    async def list(
        self,
        pagination: PaginationArgs = PaginationArgs(),
        filtering: FilteringArgs = FilteringArgs(),
        sorting: SortingArgs = SortingArgs(),
        include_deleted: bool = True,
    ) -> (int, int, int, List[dict]):
        """Perform a tag listing"""

        extra_filter = None
        if not include_deleted:
            extra_filter = {"bool": {"must_not": {"exists": {"field": "deleted"}}}}

        total_count: int = await self.count(include_deleted=include_deleted)
        limit, offset, results = await super(TagService, self).list(
            pagination, filtering, sorting, extra_filter=extra_filter
        )

        return total_count, limit, offset, results

    @audit
    @add_to_history
    async def create(self, tag_data: dict) -> tuple:
        """Create a new tag"""
        new_id = uuid4()
        tag_data = self._update_meta(tag_data)
        tag_data["author"] = self.user.username
        tag_data["created"] = tag_data["updated"]
        logger.info(f"Creating new Tag [{tag_data['name']}]", tag_id=new_id)
        new_tag = await self._index(tag_data, doc_id=new_id)

        audit_args = {
            "action": "create",
            "component": "tag",
            "message": "Creating new Tag",
            "tag_id": new_id,
            "version": new_tag["_version"],
        }

        return audit_args, new_tag

    @audit
    @add_to_history
    async def create_reference(self, tag_id: UUID, sequence: DocumentSequence, payload: dict) -> tuple:
        """Create a new reference for the supplied tag"""
        existing = await self.get(tag_id, sequence)
        existing = existing["_source"]

        logger.info("Creating new reference for Tag", tag_id=tag_id)

        # Since tags may not have a references field at all yet, we need to ensure it exists
        existing["references"] = existing.get("references", [])

        # Create a payload we pass off to our update call that contains only the top level field
        existing["references"].append(payload)

        ret = await self._index(doc_id=tag_id, body=existing, sequence=sequence)

        audit_args = {
            "action": "update",
            "component": "tag",
            "message": f"Tag [{tag_id}] had {list(payload.keys())} modified by [{self.user.username}]",
            "tag_id": tag_id,
            "version": ret["_version"],
            "subcomponent": "references",
            "subcomponent_action": "create",
        }

        return audit_args, ret

    @audit
    @add_to_history
    async def update(self, tag_id: UUID, sequence: DocumentSequence, payload: dict) -> dict:
        logger.info(f"Updating Tag base info", tag_id=tag_id)
        tag = await self.get(tag_id, sequence=sequence)
        updated_tag = tag["_source"] | payload
        updated_tag = self._update_meta(updated_tag)

        ret = await self._index(doc_id=tag_id, body=updated_tag, sequence=sequence)

        await self.audit_service.add(
            "update",
            "tag",
            f"Tag [{tag_id}] had {list(payload.keys())} modified by [{self.user.username}]",
            user=self.user.username,
            tag_id=tag_id,
            version=ret["_version"],
        )

        return ret

    @audit
    @add_to_history
    async def delete(self, tag_id: UUID, sequence: DocumentSequence) -> dict:
        logger.info(f"Deleting Tag", tag_id=tag_id)
        tag = await self.get(tag_id, sequence=sequence)
        tag = tag["_source"]
        tag = self._update_meta(tag)

        # Set our deleted field to be the same as the update timestamp to signify this tag has been deleted
        tag["deleted"] = tag["updated"]

        deleted = await self._index(doc_id=tag_id, body=tag, sequence=sequence)

        await self.audit_service.add(
            "delete",
            "tag",
            f"Tag [{tag['name']}] deleted",
            user=self.user.username,
            tag_id=tag_id,
            version=deleted["_version"],
        )

        return deleted

    @audit
    @add_to_history
    async def delete_reference(self, tag_id: UUID, sequence: DocumentSequence, reference_id: str) -> tuple:
        tag = await self.get(tag_id, sequence=sequence)
        tag = tag["_source"]

        if not tag["references"]:
            raise NotFound("No references found for the supplied Tag")

        # Remove the reference by ID
        tag["references"] = [ref for ref in tag["references"] if ref["id"] != reference_id]

        ret = await self._index(doc_id=tag_id, body=tag, sequence=sequence)

        audit_args = {
            "action": "update",
            "component": "tag",
            "message": f"Tag [{tag_id}] had reference {reference_id} deleted",
            "tag_id": tag_id,
            "version": ret["_version"],
            "subcomponent": "references",
            "subcomponent_action": "delete",
        }

        return audit_args, ret
