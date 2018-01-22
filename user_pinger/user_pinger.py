"""main classs"""
from configparser import ConfigParser, ParsingError, NoSectionError
import pickle
import logging
import signal
from typing import Deque, List

import praw
from slack_python_logging import slack_logger

class UserPinger(object):
    """pings users"""
    __slots__ = ["reddit", "subreddit", "logger", "parsed", "groups"]

    def __init__(self, reddit: praw.Reddit, subreddit: str) -> None:
        """initialize"""
        def register_signals() -> None:
            """registers signals"""
            signal.signal(signal.SIGTERM, self.exit)

        self.logger: logging.Logger = slack_logger.initialize("user_pinger")
        self.logger.debug("Initializing")
        self.reddit: praw.Reddit = reddit
        self.subreddit: praw.models.Subreddit = self.reddit.subreddit(subreddit)
        self.parsed: Deque[str] = self.load()
        self.groups: ConfigParser = self.get_groups()
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
                        self.logger.warning("Deque length is not 10000, returning new one")
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

    def get_groups(self) -> ConfigParser:
        """gets current groups"""
        groups: ConfigParser = ConfigParser(allow_no_value=True)
        groups.optionxform = lambda option: option # preserve capitalization

        self.logger.debug("Getting groups")
        import prawcore
        try:
            groups.read_string(self.subreddit.wiki["userpinger/groups"].content_md)
        except prawcore.exceptions.NotFound:
            self.logger.error("Could not find groups")
            raise
        except ParsingError:
            self.logger.exception("Malformed file, could not parse")
            raise
        except prawcore.exceptions.PrawcoreException:
            self.logger.exception("Unknown exception caught")
            raise
        self.logger.debug("Successfully got groups")
        return groups

    def listen(self) -> None:
        """lists to subreddit's comments for pings"""
        import prawcore
        from time import sleep
        try:
            for comment in self.subreddit.stream.comments(pause_after=5):
                if comment is None:
                    break
                if str(comment) in self.parsed:
                    continue
                self.handle(comment)
        except prawcore.exceptions.ServerError:
            self.logger.error("Server error: Sleeping for 1 minute.")
            sleep(60)
        except prawcore.exceptions.ResponseException:
            self.logger.error("Response error: Sleeping for 1 minute.")
            sleep(60)
        except prawcore.exceptions.RequestException:
            self.logger.error("Request error: Sleeping for 1 minute.")
            sleep(60)

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
        self.logger.debug("Handling ping")

        self.logger.debug("Updating groups")
        self.groups = self.get_groups()
        self.logger.debug("Updated groups")

        author: str = str(comment.author)
        self.logger.debug("Getting users in group")
        try:
            users: List[str] = self.groups.options(group)
        except NoSectionError:
            self.logger.warning("Group \"%s\" by %s does not exist", group, author)
            self.send_error_pm(["You pinged a group that does not exist"], comment)
            return
        self.logger.debug("Got users in group")

        self.logger.debug("Checking if author is in group")
        if author.lower() not in [user.lower() for user in users]:
            self.logger.warning("Non-member %s tried to ping \"%s\" group", author, group)
            self.send_error_pm([
                f"You need to be a member of {group} to ping it",
                "If you would like to be added, please contact the moderators"
            ], comment)
            return
        self.logger.debug("Checked that author is in group")

        self.ping_users(group, users, comment)
        return

    def send_error_pm(self, errors: List[str], comment: praw.models.Comment) -> None:
        """sends error PM"""
        self.logger.debug("Sending error PM to %s", comment.author)
        errors.append(
            "If you believe this is a mistake, please contact the moderators")
        comment.author.message(
            subject="Ping Error",
            message="\n\n".join(errors)
        )
        return

    def ping_users(self, group: str, users: List[str], comment: praw.models.Comment) -> None:
        """pings users"""
        def post_comment() -> praw.models.Comment:
            """posts reply indicating ping was successful"""
            return comment.reply(f"^(Pinging members of {group} Group...)")

        def edit_comment(posted: praw.models.Comment) -> None:
            """edits comment to reflect all users pinged"""
            users_list: str = ", ".join([f"/u/{user}" for user in users])
            body: str = "\n\n".join([
                f"^(Pinged members of {group} Group)",
                f"^({users_list})",
                "^(Contact Moderators to join this group)"
            ])
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
                self.reddit.redditor(user).message(
                    subject=f"You've been pinged by /u/{comment.author} in group {group}",
                    message=str(comment.permalink)
                )
            except praw.exceptions.APIException:
                self.logger.debug("%s could not be found, skipping", user)
        self.logger.debug("Pinged individual users")

        self.logger.debug("Editing comment")
        edit_comment(posted_comment)
        self.logger.debug("Edited comment")

        self.logger.debug("Pinged group \"%s\"")
        return
