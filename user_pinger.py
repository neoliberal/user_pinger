"""main class"""
from configparser import ConfigParser, ParsingError, NoSectionError
import logging
import pickle
import re
import string
import signal
from urllib.parse import quote
from time import sleep, time
from typing import Deque, List, Optional, Callable, Dict, Tuple

import praw

#from slack_python_logging import slack_logger


class UserPinger(object):
    """pings users"""
    __slots__ = [
        "reddit", "primary_subreddit", "subreddits", "config", "logger", "parsed", "start_time"
    ]
    # Group names must be ASCII upper case separated by '-'
    GROUP_ALLOWED_CHARS = string.ascii_uppercase + string.digits + '-' + '+'
    # Punctuation that we can strip (leading/trailing) safely when parsing a ping
    GROUP_STRIP_PUNCT = string.punctuation.replace('-', '').replace('+', '')
    MAX_PINGS_PER_COMMENT = 3


    def __init__(self, reddit: praw.Reddit, subreddit: str) -> None:
        """initialize"""

        def register_signals() -> None:
            """registers signals"""
            signal.signal(signal.SIGTERM, self.exit)

        self.logger: logging.Logger = slack_logger.initialize(
            app_name = "user_pinger",
            stream_loglevel = "INFO",
            slack_loglevel = "CRITICAL",
        )
        self.logger.setLevel("INFO")
        self.logger.debug("Initializing")
        self.reddit: praw.Reddit = reddit
        self.primary_subreddit: praw.models.Subreddit = self.reddit.subreddit(
            subreddit.split("+")[0]
        )
        self.subreddits: praw.models.Subreddit = self.reddit.subreddit(
            subreddit
        )
        self.config: ConfigParser = self._get_wiki_page(["config"])
        self.parsed: Deque[str] = self.load()
        self.start_time: float = time()
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

    def _validate_group_name(self, group_name: str) -> Tuple[bool, str]:
        if set(group_name) - set(self.GROUP_ALLOWED_CHARS):
            msg = f"Group name {group_name} must only use {self.GROUP_ALLOWED_CHARS}"
            return False, msg
        return True, None

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
            groups.read_string(self.primary_subreddit.wiki[combined_page].content_md)
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
        self.primary_subreddit.wiki[combined_page].edit(stream.getvalue(), reason=message)
        stream.close()
        self.logger.debug("Updated wiki page")
        return

    def _footer(self, commands: List[Tuple[str, ...]]) -> str:
        return ' | '.join([self._userpinger_documentation_link()] + [self._command_link(*command) for command in commands])

    def _userpinger_documentation_link(self) -> str:
        return f"[About & group list](https://reddit.com/r/{self.primary_subreddit.display_name}/wiki/userpinger/documentation)"

    def _command_link(self, name: str, header: str, action: str, data: List[str]) -> str:
        command: str = f"{action} {data}"
        return f"[{name}](https://reddit.com/message/compose?to={str(self.reddit.user.me())}&subject={quote(header)}&message={quote(command)})"

    def _send_pm(self, subject: str, body: List[str], author: praw.models.Redditor) -> None:
        """sends PM"""
        self.logger.debug("Sending PM to %s", author)
        try:
            author.message(subject=subject[:240], message="\n\n".join(body)[:240])
            self.logger.debug("Sent PM to %s", author)
        except praw.exceptions.RedditAPIException as e:
            self.logger.error("Unable to send PM to %s, exception: %s", author, e)
        return

    def _send_error_pm(self, subject: str, body: List[str], author: praw.models.Redditor) -> None:
        self.logger.debug("Sending Error PM \"%s\" to %s", subject, author)
        self._send_pm(f"Userpinger Error: {subject}", body, author)

    def listen(self) -> None:
        """lists to subreddit's comments for pings"""
        import prawcore
        try:
            for comment in self.subreddits.stream.comments(pause_after=1):
                if comment is None:
                    break
                if comment.banned_by is not None:
                    # Don't trigger on removed comments
                    continue
                if comment.created_utc < self.start_time:
                    # Don't trigger on comments posted prior to startup
                    continue
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
        return author in self.primary_subreddit.moderator()

    def handle_comment(self, comment: praw.models.Comment) -> None:
        """handles ping"""
       
        # 3. Finally, passes the first N (determined by the constant MAX_PINGS_PER_COMMENT) group names to 
        # "+" represents the "union" operator for multiple pings in the same comment.
        # "-" is not a special operator. It is just part of the name of many ping groups.

        # 1. Splits the (capitalized) comment by whitespace and +, and creates a list of substrings immediately
        #  following each substring !ping, potentially split into separate substrings divided by a "+"
        split: List[str] = comment.body.upper().split()
        self.parsed.append(str(comment))
        tokens: List[str] = []
        for index, value in enumerate(split):
            group = ""
            if (value == "!PING"):
                try:
                    group = split[index + 1]
                except IndexError:
                    break
                tokens += group.split("+")
        if not tokens:
            self.logger.debug("End of comment with no group specified")
            return
        self.logger.debug("Comment contained group tokens [%s]", ', '.join(tokens))

        valid: List[str] = []
        invalid: List[str] = []
        triggers = map(lambda x: x.strip(self.GROUP_STRIP_PUNCT), tokens)

        # Checks which of those group names are valid, and creates a list of valid group names and another of invalid group names.
        # The valid group names are passed to the handle_ping function and the invalid group names are sent in an error message to the comment author.
        for iterate_through_pings in range(MAX_PINGS_PER_COMMENT):
            msg: Optional[str]
            (is_valid_group_name, msg) = self._validate_group_name(triggers[iterate_through_pings])
            if is_valid_group:
                valid.append(triggers[iterate_through_pings])
            if msg is not None:
                self.logger.debug(msg)
        error_message: str = ""
        if (len(invalid) == 1):
            error_message += f"* The group name {invalid[0]} contains invalid characters. \n\n"
            if (len(invalid) > 1):
                error_message += f"""* The group names {", ".join(invalid[:-1]) + " and " + invalid[-1]} contain invalid characters. \n\n"""

        self.logger.debug("Pinging group(s) [%s]", ', '.join(valid))
        self.handle_ping(valid, error_message, comment)

    def handle_ping(self, groups: list[str], error_message: str, comment: praw.models.Comment) -> None:
        """handles ping"""
        self.logger.debug("Handling ping")

        self.logger.debug("Updating config")
        self.config = self._get_wiki_page(["config"])
        self.logger.debug("Updated config")

        self.logger.debug("Getting list of all groups from the wiki")
        list_of_all_groups: ConfigParser = self._get_wiki_page(["config", "groups"])
        self.logger.debug("Got list of all groups from the wiki")

        self.logger.debug("Getting users in groups")

        invalid_groups: List[str] = [] 
        nonmember_groups: List[str] = []
        existant_groups: List[str] = []
        users: List[str] = []
        for group in groups:
            members =  self.get_group_members(group, list_of_all_groups)
            if self.group_exists(group, list_of_all_groups) is False:
                invalid_groups += group
                self.logger.warning("Group \"%s\" by %s does not exist", group, comment.author)
            elif not (self.in_groups(comment.author, members) or self.public_group(group) or self.is_moderator(comment.author)):
                nonmember_groups += group
                self.logger.warning("Non-member %s tried to ping Group {%s}", comment.author, group)
            else:
                existant_groups += group
                users += members

        if len(invalid_groups) == 1:
            error_message += f"* You tried to ping group {invalid_groups[0]} that does not exist. "
        elif (len(invalid_groups > 1)):
            error_message += f"""* You tried to ping groups {", ".join(invalid_groups[:-1]) + " and " + invalid_groups[-1]} that do not exist. \n\n"""
        if len(nonmember_groups) == 1:
            error_message += f"* You need to be a member of {nonmember_groups[0]} to ping it." + self._command_link(f"Click here, then click \"send\" to join {nonmember_groups[0]}. \n\n", f"Join {nonmember_groups[0]}", "addtogroup", "+".join(nonmember_groups))
        elif (len(nonmember_groups) > 1):
            error_message += f"""* You need to be a member of {", ".join(nonmember_groups[:-1]) + " and " + nonmember_groups[-1]} to ping them. \n\n""" + self._command_link(f"""Click here, then click \"send\" to join {", ".join(nonmember_groups[:-1]) + " and " + nonmember_groups[-1]}. """, f"Join {nonmember_groups[0]}", "addtogroup", "+".join(nonmember_groups))


        users = list(set(users))
        self.logger.debug("Got users in groups")

        self.ping_users(existant_groups, users, error_message, comment)
        return 

    def ping_users(self, groups: List[str], users: List[str], error_message: str, comment: praw.models.Comment) -> None:
        """pings users"""
        if error_message:
            if not groups:
                send_error_pm(self, f"Invalid Group(s)", f"Your ping request has caused one or more errors:\n\n" + error_message, comment.author)
            elif len(groups) == 1:
                send_error_pm(self, f"Invalid Group(s)", f"Group {groups[0]} has been successfully pinged. However, your ping request has caused one or more errors:\n\n" + error_message, comment.author)
            else:
                send_error_pm(self, f"""Invalid Group(s)", f"Groups {", ".join(groups[:-1]) + " and " + groups[-1]} have been successfully pinged. However, your ping request has caused one or more errors:\n\n""" + error_message, comment.author)
        if not groups:
            return

        def post_comment() -> praw.models.Comment:
            """posts reply indicating ping was successful"""
            if len(groups) == 1:
                return comment.reply(f"^(Pinging members of {groups[0]} group...)")
            else:
                return comment.reply(f"""^(Pinging members of {", ".join(groups[:-1]) + " and " + groups[-1]} groups...)""")
  
            

        def edit_comment(posted: praw.models.Comment) -> None:
            """edits comment to reflect all users pinged"""
            if len(groups) == 1:
                body: str = "\n\n".join([f"Pinged members of {groups[0]} group.",
                    self._footer([("Subscribe to this group", f"Add yourself to group {group}", "addtogroup", f"{group}"),
                                  ("Unsubscribe from this group", f"Unsubscribe from group {group}", "unsubscribe", f"{group}"),
                                  ("Unsubscribe from all groups", f"Unsubscribe from all groups", "unsubscribe", "")])])
            else:
                body: str = "\n\n".join([f"""Pinged members of {", ".join(groups[:-1]) + " and " + groups[-1]} groups.""",
                    self._footer([("Subscribe to this group", f"""Add yourself to group(s) {"+".join(groups)}""", "addtogroup", f"""{"+".join(groups)}"""),
                                  ("Unsubscribe from this group", f"""Unsubscribe from group(s) {"+".join(groups)}""", "unsubscribe", f"""{"+".join(groups)}"""),
                                  ("Unsubscribe from all groups", f"Unsubscribe from all groups", "unsubscribe", "")])])

            posted.edit(body)

        self.logger.info("Pinging group \"%s\"", group)

        self.logger.debug("Posting comment")
        try:
            posted_comment: praw.models.Comment = post_comment()
        except praw.exceptions.APIException:
            self.logger.debug("Original ping comment was deleted. Exiting.")
            return
        self.logger.debug("Posted comment")

        self.logger.debug("Pinging individual users")
        for user in users:
            if user.lower() == str(comment.author).lower():
                continue
            try:
                for group in groups:
                    unsub_msg += self._command_link(f"^Click ^here ^to ^unsubscribe ^from ^{group}", f"Unsubscribe from group {group}", "unsubscribe", f"{group}") + "\n\n"
                    unsub_msg += self._command_link(f"^Reply ^\"unsubscribe\" ^to ^stop ^receiving ^these ^messages", "Unsubscribe from all groups", "unsubscribe", "") + "\n\n"
                self.reddit.redditor(user).message(
                    subject=f"You've been pinged by /u/{comment.author} in group {groups[0]}" if (len(groups) == 1) else f"""You've been pinged by /u/{comment.author} in group {"+".join(groups)}""",
                    message=unsub_msg
                )
            except praw.exceptions.APIException as ex:
                self.logger.debug("%s could not be found in group %s", user, group)
                # Check if account is deleted/suspended/misspelled, remove if so
                error_types = [subexception.error_type for subexception in ex.items]
                if "USER_DOESNT_EXIST" or "INVALID_USER" in error_types:
                    self.logger.debug("Account %s is deleted or suspended, removing them", user)
                    groups: ConfigParser = self._get_wiki_page(["config", "groups"])
                    regex = re.compile(user, flags=re.IGNORECASE)
                    for section in groups.sections():
                        matches = list(filter(regex.match, groups.options(section)))
                        for match in matches:
                            groups.remove_option(section, match)
                    self._update_wiki_page(["config", "groups"], groups, message=f"Removed deleted or suspended user /u/{user}")

        self.logger.debug("Pinged individual users")

        self.logger.debug("Editing comment")
        edit_comment(posted_comment)
        self.logger.debug("Edited comment")

        self.logger.info("Pinged group \"%s\"", ", ".join(groups[:-1]) + " and " + groups[-1])
        return

    def handle_command(self, message: praw.models.Message) -> None:
        body: str = message.body.lower()
        words: list = body.split()
        command: str = words[0]
        data: str = ' '.join(words[1:])
        author: praw.models.Redditor = message.author

        self.logger.info("Handling Command %s by %s", command, author)

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
            body = addtogroup [group 1]+[group 2]+...
            """
            # "addtogroup DAD+USA-CVILLE" and "addtogroup DAD, USA-CVILLE" and "addtogroup DAD,USA-CVILLE" are equivalent
            groups_to_add: List[str] = body.replace(", ", "+").replace(",","+").split("+")


            self.logger.info("Adding %s to group(s) \"%s\"", author, body)
            self.logger.debug("Getting groups")
            list_of_all_groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")
            

            error_message: str = ""
            valid_groups: List[str] = []
            for group in groups_to_add:
                self.logger.debug("Checking if group exists or is protected")
                if self.group_exists(group, list_of_all_groups) is False:
                    self.logger.debug("Group does not exist")
                    self.logger.warning(f"Add group request {group} by {author} is invalid (does not exist)")
                    error_message += f"* The group \"{group}\" does not exist.\n\n"
                elif self.protected_group(body):
                    self.logger.debug("Group is protected")
                    self.logger.warning("%s tried to add themselves to protected group \"%s\"", author, body)
                    error_message += f"* You attempted to add yourself to protected group {group}.\n\n"
                else:
                    self.logger.debug("Group exists and is not protected")
                    valid_groups.append(group.upper())
            for group in valid_groups:
                list_of_all_groups.set(group, str(author), None)
                self.logger.info("Added successfully")
                # Revision reasons cannot contain emojis. This works around that.
                revision_reason = group.replace('🔮', '[Crystal Ball]').encode('ascii', 'ignore').decode('utf-8')
                self._update_wiki_page(["config", "groups"], list_of_all_groups, f"Added /u/{author} to Group {revision_reason}")
            if len(valid_groups) == 1:
                self._send_pm(f"Added to Group {valid_group[0]}", [f"You were added to group {valid_group[0]}"], author)
            if len(valid_groups) > 1:
                self._send_pm(f"""Added to Groups {"+".join(valid_group)}""", [f"You were added to groups {group}"], author)
            return

        def remove_from_group(body: str, author: praw.models.Redditor) -> None:
            """
            Removes member from a Group

            Usage:
            body = removefromgroup [group you want to be removed from]
            body = removefromgroup [group1]+[group2]+...
            """
            groups_to_add: List[str] = body.replace(", ", "+").replace(",","+").split("+")
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            error_message: str = ""
            valid_groups: List[str] = []
            for group in groups_to_add:
                self.logger.debug("Checking if group exists or is protected")
                if self.group_exists(group, list_of_all_groups) is False:
                    self.logger.debug("Group does not exist")
                    self.logger.warning(f"Add group request {group} by {author} is invalid (does not exist)")
                    error_message += f"* The group \"{group}\" does not exist.\n\n"
                else:
                    valid_groups += group

            self.logger.debug("Removing %s from groups %s", author, "+".join(valid_groups))
            regex = re.compile(str(author), flags=re.IGNORECASE)
            for group in valid_groups:
                matches += list(filter(regex.match, groups.options(group.upper())))
            if error_message:
                self.logger.warning("Remove group request has invalid argument(s)")
                self._send_error_pm(f"Cannot remove non-member from group", [f"You could not be removed from one or more groups because you are not a member:\n\n" + error_message], author)
            if valid_groups:
                for group in valid_groups:
                    for match in matches:
                        groups.remove_option(group.upper(), match)
                self.logger.debug("Removed from group")
                if valid_groups == 1:
                    self._send_pm(f"Removed from Group {valid_groups[0].upper()}", [f"You were removed from group {valid_groups[0].upper()}"], author)
                else:
                    self._send_pm(f"""Removed from Groups {", ".join(valid_groups[:-1]) + " and " + valid_groups[-1]}""", [f"""You were removed from groups {", ".join(valid_groups[:-1]) + " and " + valid_groups[-1]}"""], author)
                    # Revision reasons cannot contain emojis. This works around that.
                revision_reason = group.replace('🔮', '[Crystal Ball]').encode('ascii', 'ignore').decode('utf-8')
                self._update_wiki_page(["config", "groups"], groups, message=f"Removed /u/{author} from Group {revision_reason}")
            return

        def unsubscribe(data: str, author: praw.models.Redditor) -> None:
            """
            Removes a user from all Groups

            Usage:
            body = unsubscribe [group_name]
            """
            data = data.upper()

            unsub_groups: List[str] = data.split()
            self.logger.info(f"Processing unsubscribe {str(author)} from {str(unsub_groups)}")
            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")
            groups_to_remove: List[str] = []
            for (group_name, usernames) in groups.items():
                for username in usernames.items():
                    username = str(username[0]).upper()
                    if (not unsub_groups or group_name in unsub_groups):
                        if str(author).upper() == username.upper():
                            self.logger.info(f"Removing {username} from {group_name}")
                            groups_to_remove.append(group_name)
                            break # Don't try to remove the same user multiple times
            remove_from_group("+".join(groups_to_remove), author)
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
            group_name: str = body.upper()
            ret: bool
            msg: Optional[str]
            ret, msg = self._validate_group_name(group_name)
            if not ret:
                self.logger.warning(msg)
                self._send_pm("Cannot create group", [msg], author)
                return

            self.logger.debug("Getting groups")
            groups: ConfigParser = self._get_wiki_page(["config", "groups"])
            self.logger.debug("Got groups")

            self.logger.debug("Checking if group exists")
            if self.group_exists(body, groups) is True:
                self.logger.warning("Attempted to make group that already exists")
                return
            self.logger.debug("Group does not exist")

            self.logger.debug("Creating group %s", group_name)
            groups.add_section(group_name)
            groups.set(group_name, str(author), None)
            self.logger.debug("Created group")

            self._send_pm(f"Created Group {group_name}", ["Group created"], author)
            self._update_wiki_page(["config", "groups"], groups, f"Created new Group {group_name}")
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

            self._send_pm(f"Removed Group {body.upper()}", ["Group removed"], author)
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
                "addtogroup": add_to_group,
                "unsubscribe": unsubscribe
            }

        mod_commands: Dict[str, Callable[[str, praw.models.Redditor], None]] = {
                "removefromgroup": remove_from_group,
                "list": list_groups,
                "help": help_command,
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
