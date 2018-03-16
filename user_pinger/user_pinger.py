"""main classs"""
from configparser import ConfigParser, ParsingError, NoSectionError
import pickle
import logging
import signal
from typing import Deque, List, Optional

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
        self.config: ConfigParser = self.get_wiki_page("config")
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

    def get_wiki_page(self, page: Optional[str] = None) -> ConfigParser:
        """gets current groups"""
        groups: ConfigParser = ConfigParser(allow_no_value=True)
        groups.optionxform = lambda option: option  # type: ignore

        combined_page: str = '/'.join(filter(None, ["userpinger", page]))
        self.logger.debug("Getting wiki page \"%s\"", combined_page)
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

    def update_wiki_page(self, page: str, groups: ConfigParser, message: str) -> None:
        """updates wiki page with new groups"""
        import io
        self.logger.debug("Updating wiki page")
        stream: io.StringIO = io.StringIO()
        groups.write(stream)
        self.subreddit.wiki[f"userpinger/{page}"].edit(stream.getvalue(), reason=message)
        stream.close()
        self.logger.debug("Updated wiki page")
        return

    def listen(self) -> None:
        """lists to subreddit's comments for pings"""
        import prawcore
        from time import sleep
        try:
            for comment in self.subreddit.stream.comments(pause_after=1):
                if comment is None:
                    break
                if str(comment) in self.parsed:
                    continue
                self.handle(comment)
            for message in self.reddit.inbox.unread(limit=1):
                if isinstance(
                        message,
                        praw.models.Message) and message.subject == "addgroup":
                    self.add_to_group(message)
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

    def get_group_members(self, request: str,
                          groups: ConfigParser) -> Optional[List[str]]:
        try:
            users: List[str] = groups.options(request)
        except NoSectionError:
            return None
        else:
            return users

    def handle(self, comment: praw.models.Comment) -> None:
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

        def in_group(author: praw.models.Redditor, users: List[str]) -> bool:
            """checks if author belongs to group"""
            return str(author).lower() in [user.lower() for user in users]

        def public_group(group: str) -> bool:
            """checks if group is public, and can be pinged by anyone"""
            return group.lower() in self.config.options("public")

        def is_moderator(author: praw.models.Subreddit) -> bool:
            """checks if author is a moderator"""
            return author in self.subreddit.moderator()

        self.logger.debug("Handling ping")

        self.logger.debug("Updating config")
        self.config = self.get_wiki_page("config")
        self.logger.debug("Updated config")

        self.logger.debug("Getting groups")
        groups: ConfigParser = self.get_wiki_page("groups")
        self.logger.debug("Got groups")

        self.logger.debug("Getting users in group")
        users: Optional[List[str]] = self.get_group_members(group, groups)
        if users is None:
            self.logger.warning("Group \"%s\" by %s does not exist", group,
                                comment.author)
            self.send_error_pm(
                [f"You pinged group {group} that does not exist"],
                comment.author)
            return
        self.logger.debug("Got users in group")

        self.logger.debug("Checking if author is in group or group is public")
        if not (in_group(comment.author, users) or public_group(group)
                or is_moderator(comment.author)):
            self.logger.warning("Non-member %s tried to ping \"%s\" group",
                                comment.author, group)
            self.send_error_pm([
                f"You need to be a member of {group} to ping it",
                "If you would like to be added, please contact the moderators"
            ], comment.author)
            return
        self.logger.debug("Checked that author is in group")

        self.ping_users(group, users, comment)
        return

    def send_error_pm(self, errors: List[str],
                      author: praw.models.Redditor) -> None:
        """sends error PM"""
        self.logger.debug("Sending error PM to %s", author)
        errors.append(
            "If you believe this is a mistake, please contact the moderators")
        author.message(subject="Ping Error", message="\n\n".join(errors))
        return

    def ping_users(self, group: str, users: List[str],
                   comment: praw.models.Comment) -> None:
        """pings users"""

        def post_comment() -> praw.models.Comment:
            """posts reply indicating ping was successful"""
            return comment.reply(f"^(Pinging members of {group} group...)")

        def edit_comment(posted: praw.models.Comment) -> None:
            """edits comment to reflect all users pinged"""
            body: str = "\n\n".join(
                [f"Pinged members of {group} group.", "---",
                 make_footer()])
            posted.edit(body)

        def make_footer() -> str:
            """"make footer for comment"""
            return (
                "[user_pinger](https://github.com/neoliberal/user_pinger) | "
                f"[Request](https://reddit.com/message/compose?to={str(self.reddit.user.me())}&subject=addgroup&message={group})"
                " to join this group")

        self.logger.debug("Pinging group")

        self.logger.debug("Posting comment")
        posted_comment: praw.models.Comment = post_comment()
        self.logger.debug("Posted comment")

        self.logger.debug("Pinging individual users")
        for user in users:
            if user.lower() == str(comment.author).lower():
                continue
            try:
                self.reddit.redditor(user).message(
                    subject=
                    f"You've been pinged by /u/{comment.author} in group {group}",
                    message=f"[Click here to view the comment](https://www.reddit.com{str(comment.permalink)}?context=1000)")
            except praw.exceptions.APIException:
                self.logger.debug("%s could not be found in group %s, skipping", user, group)
        self.logger.debug("Pinged individual users")

        self.logger.debug("Editing comment")
        edit_comment(posted_comment)
        self.logger.debug("Edited comment")

        self.logger.debug("Pinged group \"%s\"", group)
        return

    def add_to_group(self, message: praw.models.Message) -> None:
        """adds member to group"""

        def protected_group(group: str) -> bool:
            """check if group is protected and can only be added to by moderators"""
            return group.lower() in self.config.options("protected")

        self.logger.debug("Handling addgroup request")

        self.logger.debug("Getting groups")
        groups: ConfigParser = self.get_wiki_page("groups")
        self.logger.debug("Got groups")

        self.logger.debug("Checking if request exists")
        if self.get_group_members(message.body, groups) is None:
            self.logger.warning(
                f"Add group request {message.body} by {message.author} is invalid"
            )
            self.send_error_pm(
                ["Your add group request {message.body} is invalid"],
                message.author)
            return
        self.logger.debug("Added member to group")

        self.logger.debug("Checking if group is protected")
        if protected_group(message.body):
            self.logger.warning(
                "%s tried to add themselves to protected group \"%s\"",
                message.author, message.body)
            self.send_error_pm([
                f"You attempted to add yourself to protected group {message.body}.",
                "Contact moderators to be added."
            ], message.author)
            return

        self.logger.debug("Adding %s to group \"%s\"", message.author,
                          message.body)
        groups.set(message.body, str(message.author), None)
        message.author.message(
            subject="Added to group",
            message=f"You've been added to group {message.body}")
        self.logger.debug("Added successfully")

        self.update_wiki_page("groups", groups, f"Added /u/{message.author} to Group {message.body}")
        return
