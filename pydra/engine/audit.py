"""Module to keep track of provenance information."""

import os
import typing as ty
import json
from pydra.utils.messenger import send_message, make_message, gen_uuid, now, AuditFlag
from pydra.utils.general import attrs_values
from fileformats.core import FileSet
from pydra.utils.hash import hash_function

try:
    import importlib_resources
except ImportError:
    import importlib.resources as importlib_resources  # type: ignore

if ty.TYPE_CHECKING:
    from pydra.engine.job import Job


class Audit:
    """Handle provenance tracking and resource utilization."""

    def __init__(self, audit_flags, messengers, messenger_args, develop=None):
        """
        Initialize the auditing functionality.

        Parameters
        ----------
        audit_flags : :class:`AuditFlag`
            Base configuration of auditing.
        messengers : :obj:`pydra.util.messenger.Messenger`
            or list of :class:`pydra.util.messenger.Messenger`, optional
            Taskify types of messenger used by Audit to send a message.
            Could be `PrintMessenger`, `FileMessenger`, or `RemoteRESTMessenger`.
        messenger_args : :obj:`dict`, optional
            Optional arguments for the `Messenger.send` method.
        develop : :obj:`bool`, optional
            If True, the local context.jsonld file is used, otherwise the one from github is used.

        """
        from pydra.utils.general import ensure_list

        self.audit_flags = audit_flags
        self.messengers = ensure_list(messengers)
        self.messenger_args = messenger_args
        self.develop = develop

    def start_audit(self, odir):
        """
        Start recording provenance.

        Monitored information is not sent until directory is created,
        in case message directory is inside job output directory.

        Parameters
        ----------
        odir : :obj:`os.pathlike`
            Message output directory.

        """
        self.odir = odir
        if self.audit_check(AuditFlag.PROV):
            self.aid = f"uid:{gen_uuid()}"

            user_id = f"uid:{gen_uuid()}"
            start_message = {
                "@id": self.aid,
                "@type": "job",
                "startedAtTime": now(),
                "executedBy": user_id,
            }

        os.chdir(self.odir)
        if self.audit_check(AuditFlag.PROV):
            self.audit_message(start_message, AuditFlag.PROV)
        if self.audit_check(AuditFlag.RESOURCE):
            from pydra.utils.profiler import ResourceMonitor

            self.resource_monitor = ResourceMonitor(os.getpid(), logdir=self.odir)

    def monitor(self):
        """Start resource monitoring."""
        if self.audit_check(AuditFlag.RESOURCE):
            self.resource_monitor.start()
            if self.audit_check(AuditFlag.PROV):
                self.mid = f"uid:{gen_uuid()}"
                self.audit_message(
                    {
                        "@id": self.mid,
                        "@type": "monitor",
                        "startedAtTime": now(),
                        "wasStartedBy": self.aid,
                    },
                    AuditFlag.PROV,
                )

    def finalize_audit(self, result):
        """End auditing."""
        if self.audit_check(AuditFlag.RESOURCE):
            from pydra.engine.result import gather_runtime_info

            self.resource_monitor.stop()
            result.runtime = gather_runtime_info(self.resource_monitor.fname)
            if self.audit_check(AuditFlag.PROV):
                self.audit_message(
                    {"@id": self.mid, "endedAtTime": now(), "wasEndedBy": self.aid},
                    AuditFlag.PROV,
                )
                # audit resources/runtime information
                self.eid = f"uid:{gen_uuid()}"
                entity = attrs_values(result.runtime)
                entity.update(
                    **{
                        "@id": self.eid,
                        "@type": "runtime",
                        "prov:wasGeneratedBy": self.aid,
                    }
                )
                self.audit_message(entity, AuditFlag.PROV)
                self.audit_message(
                    {
                        "@type": "prov:Generation",
                        "entity_generated": self.eid,
                        "hadActivity": self.mid,
                    },
                    AuditFlag.PROV,
                )
            self.resource_monitor = None
        if self.audit_check(AuditFlag.PROV):
            # audit outputs
            self.audit_message(
                {"@id": self.aid, "endedAtTime": now(), "errored": result.errored},
                AuditFlag.PROV,
            )

    def audit_message(self, message, flags=None):
        """
        Send auditing message.

        Parameters
        ----------
        message : :obj:`dict`
            A message in Pydra is a JSON-LD message object.
        flags : :obj:`bool`, optional
            If True and self.audit_flag, the message is sent.

        """
        if self.develop:
            context_file = importlib_resources.files("pydra") / "schema/context.jsonld"
            with context_file.open() as fp:
                context = json.load(fp)
        else:
            context = {
                "@context": "https://raw.githubusercontent.com/nipype/pydra"
                "/master/pydra/schema/context.jsonld"
            }
        if self.audit_flags & flags:
            if self.messenger_args:
                send_message(
                    make_message(message, context=context),
                    messengers=self.messengers,
                    **self.messenger_args,
                )
            else:
                send_message(
                    make_message(message, context=context), messengers=self.messengers
                )

    def audit_check(self, flag):
        """
        Determine whether auditing is enabled for a particular flag.

        Parameters
        ----------
        flag : :obj: `bool`
            The flag that is checked.

        Returns
        -------
        bool
            Boolean AND for self.oudit_flags and flag
        """
        return self.audit_flags & flag

    def audit_task(self, job: "Job"):
        import subprocess as sp
        from pydra.utils.general import get_fields

        label = job.name

        command = job.task.cmdline if hasattr(job.task, "executable") else None
        attr_list = get_fields(job.task)
        for attrs in attr_list:
            input_name = attrs.name
            value = job.inputs[input_name]
            if isinstance(value, FileSet):
                input_path = os.path.abspath(value)
                file_hash = hash_function(value)
                entity_id = f"uid:{gen_uuid()}"
                entity_message = {
                    "@id": entity_id,
                    "Label": input_name,
                    "AtLocation": input_path,
                    "GeneratedBy": None,
                    "@type": "input",
                    "digest": file_hash,
                }
                self.audit_message(entity_message, AuditFlag.PROV)

        if command is not None:
            cmd_name = command.split()[0]
            software = f"{cmd_name} --version"
            # take the first word of command as the
            # name of the executable
            # (this may not always be the case)
            version_cmd = sp.run(software, shell=True, stdout=sp.PIPE).stdout.decode(
                "utf-8"
            )
            try:
                version_cmd = version_cmd.splitlines()[0]

            except IndexError:
                version_cmd = f"{cmd_name} -- Version unknown"

        else:
            version_cmd = None

        start_message = {
            "@id": self.aid,
            "@type": "job",
            "Label": label,
            "Command": command,
            "StartedAtTime": now(),
            "AssociatedWith": version_cmd,
        }

        self.audit_message(start_message, AuditFlag.PROV)
