"""main class"""
from configparser import ConfigParser, ParsingError, NoSectionError
import pickle
import logging
import signal
from typing import Deque, List, Optional, Callable, Dict, Tuple

import praw
from slack_python_logging import slack_logger


class UserPinger(object):
    """pings users"""
    __slots__ = ["reddit", "subreddit", "config", "logger", "parsed"]

    def __init__(self, reddit: praw.Reddit, subreddit: str) -> None:
        """initialize"""

        def register_signals() -> None:
            """registers signals"""
            signal.signal(signal.SIGTERM, self.exit)

        self.logger: logging.Logger = slack_logger.initialize("user_pinger")
        self.logger.debug("Initializing")
        self.reddit: praw.Reddit = reddit
        self.subreddit: praw.models.Subreddit = self.reddit.subreddit(
            subreddit)
        self.config: ConfigParser = self._get_wiki_page(["config"])
        self.parsed: Deque[str] = self.load()
        register_signals()
        self.logger.info("Successfully initialized")

    def exit(self, signum: int, frame) -> None:
        """defines exit function"""
        import os
        _ = frame
        self.save()
        self.logger.info("Exited gracefully with signal %s", signum)
        os._exit(os.EX_OK)
        return

    def load(self) -> Deque[str]:
        """loads pickle if it exists"""
        self.logger.debug("Loading pickle file")
        try:
            with open("parsed.pkl", 'rb') as parsed_file:
                try:
                    parsed: Deque[str] = pickle.loads(parsed_file.read())
                    self.logger.debug("Loaded pickle file")
                    self.logger.debug("Current Size: %s", len(parsed))
                    if parsed.maxlen != 10000:
                        self.logger.warning(
                            "Deque length is not 10000, returning new one")
                        return Deque(parsed, maxlen=10000)
                    self.logger.debug("Maximum Size: %s", parsed.maxlen)
                    return parsed
                except EOFError:
                    self.logger.debug("Empty file, returning empty deque")
                    return Deque(maxlen=10000)
        except FileNotFoundError:
            self.logger.debug("No file found, returning empty deque")
            return Deque(maxlen=10000)

    def save(self) -> None:
        """pickles tracked comments after shutdown"""
        self.logger.debug("Saving file")
        with open("parsed.pkl", 'wb') as parsed_file:
            parsed_file.write(pickle.dumps(self.parsed))
            self.logger.debug("Saved file")
            return
        return

    def _make_userpinger_wiki_page(self, page: Optional[List[str]] = None) -> str:
        """takes a list of pages and returns a completed one"""
        combined_page: str = '/'.join(filter(None, ["userpinger"] + page))
        self.logger.debug("Getting wiki page \"%s\"", combined_page)
        return combined_page

    def _get_wiki_page(self, page: Optional[List[str]] = None) -> ConfigParser:
        """gets current groups"""
        groups: ConfigParser = ConfigParser(allow_no_value=True)
        groups.optionxform = lambda option: option  # type: ignore

        combined_page: str = self._make_userpinger_wiki_page(page)
        import prawcore
        try:
            groups.read_string(self.subreddit.wiki[combined_page].content_md)
        except prawcore.exceptions.NotFound:
            self.logger.error("Could not find groups")
            raise
        except ParsingError:  # type: ignore
            self.logger.exception("Malformed file, could not parse")
            raise
        except prawcore.exceptions.PrawcoreException:
            self.logger.exception("Unknown exception caught")
            raise
        self.logger.debug("Successfully got groups")
        return groups

    def _update_wiki_page(self, page: Optional[List[str]], config: ConfigParser, message: str) -> None:
        """updates wiki page with new groups"""
        import io
        self.logger.debug("Updating wiki page")
        stream: io.StringIO = io.StringIO()
        config.write(stream)
        combined_page: str = self._make_userpinger_wiki_page(page)
        self.subreddit.wiki[combined_page].edit(stream.getvalue(), reason=message)
        stream.close()
        self.logger.debug("Updated wiki page")
        return

    def _footer(self, commands: List[Tuple[str, ...]]) -> str:
        return ' | '.join([self._userpinger_github_link()] + [self._command_link(*command) for command in commands])

    def _userpinger_github_link(self) -> str:
        return "[user_pinger](https://github.com/neoliberal/user_pinger)"

    def _command_link(self, name: str, header: str, action: str, data: str) -> str:
        command: str = f"{action} {data}"
        return f"[{name}](https://reddit.com/message/compose?to={str(self.reddit.user.me())}&subject={header}&message={command})"

    def _send_pm(self, subject: str, body: List[str], author: praw.models.Redditor) -> None:
        """sends PM"""
        self.logger.debug("Sending PM to %s", author)
        author.message(subject=subject[:240], message="\n\n".join(body)[:240])
        self.logger.debug("Sent PM to %s", author)
        return

    def _send_error_pm(self, subject: str, body: List[str], author: praw.models.Redditor) -> None:
        self.logger.debug("Sending Error PM \"%s\" to %s", subject, author)
        self._send_pm(f"Userpinger Error: {subject}", body, author)

    def listen(self) -> None:
        """lists to subreddit's comments for pings"""
        import prawcore
        from time import sleep, time
        try:
            for comment in self.subreddit.stream.comments(pause_after=1):
                if comment is None:
                    break
                if str(comment) in self.parsed:
                    continue
                self.handle_comment(comment)
            for message in self.reddit.inbox.unread(limit=1):
                if isinstance(message, praw.models.Message):
                    if message.author is None:
                        message.mark_read()
                        continue
                    self.handle_command(message)
                    message.mark_read()
        except prawcore.exceptions.ServerError:
            self.logger.error("Server error: Sleeping for 1 minute.")
            sleep(60)
        except prawcore.exceptions.ResponseException:
            self.logger.error("Response error: Sleeping for 1 minute.")
            sleep(60)
        except prawcore.exceptions.RequestException:
            self.logger.error("Request error: Sleeping for 1 minute.")
            sleep(60)

    def group_exists(self, request: str, groups: ConfigParser) -> bool:
        """checks if group is in config"""
        return groups.has_section(request.upper())

    def in_group(self, author: praw.models.Redditor, users: List[str]) -> bool:
        """checks if author belongs to group"""
        return str(author).lower() in [user.lower() for user in users]

    def get_group_members(self, request: str, groups: ConfigParser) -> List[str]:
        """returns members of group"""
        return groups.options(request.upper())

    def public_group(self, group: str) -> bool:
        """checks if group is public, and can be pinged by anyone"""
        return group.lower() in self.config.options("public")

    def protected_group(self, group: str) -> bool:
        """check if group is protected and can only be added to by moderators"""
        return group.lower() in self.config.options("protected")

    def is_moderator(self, author: praw.models.Subreddit) -> bool:
        """checks if author is a moderator"""
        return author in self.subreddit.moderator()

    def handle_comment(self, comment: praw.models.Comment) -> None:
        """handles ping"""
        split: List[str] = comment.body.upper().split()
        self.parsed.append(str(comment))

        try:
            index: int = split.index("!PING")
        except ValueError:
            # no trigger
            return
        else:
            self.logger.debug("Ping found in %s", str(comment))
            try:
                trigger: str = split[index + 1]
            except IndexError:
                self.logger.debug("End of comment with no group specified")
                return
            else:
                self.logger.debug("Found group is %s", trigger)
                self.handle_ping(trigger, comment)

    def handle_ping(self, group: str, comment: praw.models.Comment) -> None:
        """handles ping"""
        self.logger.debug("Handling ping")

        self.logger.debug("Updating config")
        self.config = self._get_wiki_page(["config"])
        self.logger.debug("Updated config")

        self.logger.debug("Getting groups")
        groups: ConfigParser = self._get_wiki_page(["config", "groups"])
        self.logger.debug("Got groups")

        self.logger.debug("Getting users in group")

        if self.group_exists(group, groups) is False:
            self.logger.warning("Group \"%s\" by %s does not exist", group, comment.author)
            self._send_pm("Invalid Ping", [f"You pinged group {group} that does not exist"], comment.author)
            return
        users: Optional[List[str]] = self.get_group_members(group, groups)
        self.logger.debug("Got users in group")

        self.logger.debug("Checking if author is in group or group is public")
        if not (self.in_group(comment.author, users) or self.public_group(group) or self.is_moderator(comment.author)):
            self.logger.warning("Non-member %s tried to ping \"%s\" group", comment.author, group)
            self._send_error_pm(f"Cannot ping Group {group}", [f"You need to be a member of {group} to ping it", self._command_link(f"Click here, then click \"send\" to join {group}", f"Join {group}", "addtogroup", f"{group}") ], comment.author)
            return
        self.logger.debug("Checked that author is in group")

        self.ping_users(group, users, comment)
        return

    def ping_users(self, group: str, users: List[str], comment: praw.models.Comment) -> None:
        """pings users"""

        def post_comment() -> praw.models.Comment:
            """posts reply indicating ping was successful"""
            return comment.reply(f"^(Pinging members of {group} group...)")

        def edit_comment(posted: praw.models.Comment) -> None:
            """edits comment to reflect all users pinged"""
            body: str = "\n\n".join([f"Pinged members of {group} group.", "---",
                self._footer([("Request to be added to this group", f"Add yourself to group {group}", "addtogroup", f"{group}"),
                              ("Unsubscribe from this group", f"Unsubscribe from group {group}", "unsubscribe", f"{group}"),
                              ("Unsubscribe from all pings", f"Unsubscribe from all groups", "unsubscribe", "")])])
            posted.edit(body)

        self.logger.debug("Pinging group")

        self.logger.debug("Posting comment")
        posted_comment: praw.models.Comment = post_comment()
        self.logger.debug("Posted comment")

        self.logger.debug("Pinging individual users")
        for user in users:
            if user.lower() == str(comment.author).lower():
                continue
            try:
                unsub_group_msg: str = self._command_link(f"^Click ^here ^to ^unsubscribe ^from ^{group}", f"Unsubscribe from group {group}", "unsubscribe", f"{group}")
                unsub_all_msg: str = self._command_link(f"^Reply ^\"unsubscribe\" ^to ^stop ^receiving ^these ^messages", "Unsubscribe from all groups", "unsubscribe", "")
                self.reddit.redditor(user).message(
                    subject=f"You've been pinged by /u/{comment.author} in group {group}",
                    message=f"[Click here to view the comment](https://www.reddit.com{str(comment.permalink)}?context=1000)\n\n---\n\n{unsub_group_msg}\n\n{unsub_all_msg}"
                )
            except praw.exceptions.APIException:
                self.logger.debug("%s could not be found in group %s, skipping", user, group)
        self.logger.debug("Pinged individual users")

        self.logger.debug("Editing comment")
        edit_comment(posted_comment)
        self.logger.debug("Edited comment")

        self.logger.debug("Pinged group \"%s\"", group)
        return

    def handle_command(self, message: praw.models.Message) -> None:
        body: str = message.body.lower()
        words: list = body.split()
        command: str = words[0]
        data: str = ' '.join(words[1:])
        author: praw.models.Redditor = message.author

        self.logger.debug("Handling Command %s by %s", command, author)

        self.logger.debug("Updating config")
        self.config = self._get_wiki_page(["config"])
        self.logger.debug("Updated config")

        self.logger.debug("Checking if command is valid")

        public_commands: list = self.config.options("commands")
        mod_commands: list = self.config.options("mod_commands")
        all_commands: list = public_commands + mod_commands

        self.logger.debug("All commands: " + str(all_commands))

        if command in all_commands:
            self.logger.debug("Checking if command is mod-only by non-moderator")
            is_mod: bool = self.is_moderator(author)

            if command in mod_commands and not is_mod:
                self._send_error_pm("Mod-only Command", [f"Your command {command} is mod-only"], author)
                return

            self.logger.debug("Command is not mod-only by non-moderator")
            self.run_command(author, is_mod, command, data)
        else:
            self._send_error_pm("Invalid Command", [f"Your command {command} was invalid"], author)

        return

    def run_command (
        self,
        author: praw.models.Redditor,
        mod: bool,
        command: str, # Command heading (formerly the subject)
        data: str # Command data (group, etc)
    ) -> None:
        def help_command(_, author: praw.models.Redditor) -> None:
            """
            Gets all avaliable commands to the user

            Usage:
            body = help
            """
            self.logger.debug("Setting appropriate commands")
            if mod:
                help_commands = {**public_commands, **mod_commands}
            else:
                help_commands = public_commands
            self.logger.debug("Set appropriate commands")

            self.logger.debug("Creating list of commands")
            commands_list: List[str] = [
                f"##{name}\n\n{function.__doc__}"
                for name, function in help_commands.items()
            ]
            self.logger.debug("Set list of commands")

            self._send_pm("Userpinger Commands", commands_list, author)
            return

        def add_to_group(body: str, author: praw.models.Redditor) -> None:
            """
            Adds member to a Group

            Usage:
            body = addtogroup [group you want to be added to]
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is False:
                self.logger.warning(f"Add group request {body} by {author} is invalid (does not exist)")
                self._send_error_pm("Invalid add group request", [f"The group \"{body}\" does not exist"], author)
                return
            self.logger.debug("Group exists")

            self.logger.debug("Checking if group is protected")
            if self.protected_group(body):
                self.logger.warning("%s tried to add themselves to protected group \"%s\"", author, body)
                self._send_error_pm("Attempted to add to protected group", [f"You attempted to add yourself to protected group {body}."], author)
                return
            self.logger.debug("Group is not protected")

            self.logger.debug("Adding %s to group \"%s\"", author, body)
            groups.set(body.upper(), str(author), None)
            self.logger.debug("Added successfully")

            self._send_pm(f"Added to Group {body.upper()}", [f"You were added to group {body.upper()}"], author)
            self._update_wiki_page(["config", "groups"], groups, f"Added /u/{author} to Group {body.upper()}")
            return

        def remove_from_group(body: str, author: praw.models.Redditor) -> None:
            """
            Removes member from a Group

            Usage:
            body = removefromgroup [group you want to be removed from]
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is None:
                self.logger.warning(f"Remove group request {body} by {author} is invalid")
                self._send_error_pm(f"Group {body} does not exist", [f"You attempted to remove yourself from group {body} which does not exist"], author)
                return
            self.logger.debug("Group exists")

            self.logger.debug("Removing %s from group %s", author, body)
            result: bool = groups.remove_option(body.upper(), str(author))

            if result is False:
                self.logger.warning("Remove group request is invalid")
                self._send_error_pm(f"Cannot remove non-member from {body}", [f"You could not be removed from group {body} because you are not a member"], author)
            else:
                self.logger.debug("Removed from group")
                self._send_pm(f"Removed from Group {body.upper()}", [f"You were removed from group {body.upper()}"], author)
                self._update_wiki_page(["config", "groups"], groups, message=f"Removed /u/{author} from Group {body}")

            return

        def unsubscribe(data: str, author: praw.models.Redditor) -> None:
            """
            Removes a user from all Groups

            Usage:
            body = unsubscribe [group_name]
            """
            data = data.upper()
            unsub_groups: List[str] = data.split()
            self.logger.debug(f"Processing unsubscribe {str(author)} from {str(unsub_groups)}")
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")
            for (group_name, usernames) in groups.items():
                for username in usernames.items():
                    username = str(username[0]).upper()
                    if (not unsub_groups or group_name in unsub_groups):
                        if str(author).upper() == username.upper():
                            self.logger.debug(f"Removing {username} from {group_name}")
                            remove_from_group(group_name, author)
            return

        def list_groups(_, author: praw.models.Redditor) -> None:
            """
            Returns a List of all available groups

            Usage:
            body = list
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Joining groups")
            groups_list: str = ', '.join(groups.sections())
            self.logger.debug("Joined groups")

            self._send_pm("Avaliable Groups", [groups_list], author)
            return

        def protect_group(body: str, author: praw.models.Redditor) -> None:
            """
            Protects a group [mod-only]

            Usage:
            body = protectgroup [group to be protected]
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is False:
                self.logger.warning("Attempted to protect non-existent group")
                return
            self.logger.debug("Group exists")

            self.logger.debug("Protecting group")
            self.config.set("protected", body, None)
            self.logger.debug("Protected group")

            self._update_wiki_page(["config"], self.config, f"Made Group {body} protected")
            return

        def unprotect_group(body: str, author: praw.models.Redditor) -> None:
            """
            Unprotects a group [mod-only]

            Usage:
            body = unprotectgroup [group to be unprotected]
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is False:
                self.logger.warning("Attempted to unprotect non-existent group")
                return
            self.logger.debug("Group exists")

            self.logger.debug("Making group unprotected")
            self.config.remove_option("protected", body)
            self.logger.debug("Made group unprotected")

            self._update_wiki_page(["config"], self.config, f"Made Group {body} unprotected")
            return

        def make_public_group(body: str, author: praw.models.Redditor) -> None:
            """
            Makes group public (if not already) [mod-only]

            Usage:
            body = makepublicgroup [group to be made public]
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is False:
                self.logger.warning("Attempted to protect non-existent group")
                return
            self.logger.debug("Group exists")

            self.logger.debug("Making group public")
            self.config.set("public", body, None)
            self.logger.debug("Made group public")

            self._update_wiki_page(["config"], self.config, f"Made Group {body} public")
            return

        def make_private_group(body: str, author: praw.models.Redditor) -> None:
            """
            Makes group private (if not already) [mod-only]

            Usage:
            body = makeprivategroup [group to be made private]
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is False:
                self.logger.warning("Attempted to make non-existent group private")
                return
            self.logger.debug("Group exists")

            self.logger.debug("Making group private")
            self.config.remove_option("private", body)
            self.logger.debug("Made group private")

            self._update_wiki_page(["config"], self.config, f"Made Group {body} private")
            return

        def create_group(body: str, author: praw.models.Redditor) -> None:
            """
            Creates group (if it doesn't exist) [mod-only]

            Usage:
            body = creategroup [group to create]

            Note:
            Moderator will be a member of the group
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is True:
                self.logger.warning("Attempted to make group that already exists")
                return
            self.logger.debug("Group exists")

            self.logger.debug("Creating group %s", body.upper())
            groups.add_section(body.upper())
            groups.set(body.upper(), str(author), None)
            self.logger.debug("Created group")

            self._send_pm(f"Created Group {body.upper()}", ["Group created"], author)
            self._update_wiki_page(["config", "groups"], groups, f"Created new Group {body.upper()}")
            return

        def delete_group(body: str, author: praw.models.Redditor) -> None:
            """
            Delete group (if it exists) [mod-only]

            Usage:
            body = deletegroup [group to be made public]
            """
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is False:
                self.logger.warning("Attempted to delete group that does not exists")
                return
            self.logger.debug("Group exists")

            self.logger.debug("Removing group %s", body)
            groups.remove_section(body.upper())
            self.logger.debug("Removed group")

            self._send_pm(f"Removed Group {body.upper}", ["Group removed"], author)
            self._update_wiki_page(["config", "groups"], groups, f"Removed Group {body.upper()}")
            return

        def add_user_to_group(body: str, author: praw.models.Redditor) -> None:
            """
            Adds user to group (if it exists) [mod-only]

            Usage:
            body = addusertogroup [group to add to], [user]
            """
            return

        def remove_user_from_group(body: str, author: praw.models.Redditor) -> None:
            """
            Removes user from group (if it exists) [mod-only]

            Usage:
            body = removeuserfromgroup [group to remove from], [user]
            """
            return

        public_commands: Dict[str, Callable[[str, praw.models.Redditor], None]] = {
                "help": help_command,
                "addtogroup": add_to_group,
                "removefromgroup": remove_from_group,
                "unsubscribe": unsubscribe,
                "list": list_groups
            }

        mod_commands: Dict[str, Callable[[str, praw.models.Redditor], None]] = {
                "protectgroup": protect_group,
                "unprotectgroup": unprotect_group,
                "makepublicgroup": make_public_group,
                "makeprivategroup": make_private_group,
                "creategroup": create_group,
                "deletegroup": delete_group,
                "addusertogroup": add_user_to_group,
                "removeuserfromgroup": remove_user_from_group,
            }

        if mod:
            {**public_commands, **mod_commands}[command](data, author)
        else:
            public_commands[command](data, author)

        return

