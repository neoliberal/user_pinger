"""service file"""
import os

import praw

from user_pinger import UserPinger

def main() -> None:
    """main service function"""

    reddit: praw.Reddit = praw.Reddit(
        client_id=os.environ["userpinger_client_id"],
        client_secret=os.environ["userpinger_client_secret"],
        refresh_token=os.environ["userpinger_refresh_token"],
        user_agent="linux:userpinger:v1.1 (by /r/Neoliberal)"
    )

    bot: UserPinger = UserPinger(
        reddit,
        os.environ["userpinger_subreddit"]
    )

    while True:
        bot.listen()

    return

if __name__ == "__main__":
    main()
