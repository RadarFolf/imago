# Copyright (C) Mila Software Group 2018-2020
# -------------------------------------------
# This is the main file, only ever run this file.
# To start in debug mode, pass 'debug' after the file name.

# Introduction to commenting
# --------------------------
# All comments within these files will be surrounded by lines to make them stand out.

# = will mark a heading of a particular process
# - will mark a sub-heading of a particular process
# ~ will mark an explanation of the code directly below

# =====================
# Import PATH libraries
# =====================
# -----------------
# Builtin libraries
# -----------------
import asyncio, logging, sys, typing, os

from atexit import register
from datetime import datetime

# ======================
# Import local libraries
# ======================
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# first, we need to add imagoweb to the current PATH,
# we do this for clarity and vanity reasons, we could
# technically just remove the imagoweb but that looks ugly
# and could interfere with other packages that may have the same name
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
sys.path.append("..")

from imagoweb.util import console, constants
from imagoweb.util.constants import cache, config
from imagoweb.util.blueprints import upload, user

# ==============================
# Check for missing dependencies
# ==============================
try:
    import attrdict, psycopg2, yaml

    from flask import Flask
    from honeybadger.contrib import FlaskHoneybadger
    from pyfiglet import print_figlet

    from gevent.pywsgi import WSGIServer

except ImportError as error:
    console.fatal(text=f"{error.name} dependency is missing.")
    
    os._exit(status=2)

app = Flask(import_name="Imago")
constants.app = app

class Imago:
    """This class handles the logic behind Mila's webserver.
    
    The core itself has many useful features, including an exit handler, plugin loader and the start logic.
    
    Plugins
    -------
    
    When the word "plugin" is seen in this class, we are referring to additional Python files. 
    Akin to discord.py cogs, these plugins contain different routes within them, as a sort of categorising system.
    
    """

    def exit(self):
        """Executed whenever the process exists. 
        This tends to be through KeyboardInterrupt or invoked shutdown.
        
        This function won't be triggered if we exited via::
            A signal not handled by Python;
            A fatal internal error within Python (interpreter crash);
            When os._exit is called (such as in the safe_close function)
            
        We can also call this ourselves, allowing us to close properly without repeating code."""

        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        # this removes the annoying ^C character
        # that appears whenever you use a keyboard interruption
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        sys.stderr.write("\r")

        console.fatal(text="Shutting down...")

        # ====================
        # Dump console to file
        # ====================
        now = datetime.utcnow()

        log_dir = f"logs/stdout/{now.strftime(format='%Y-%m')}/{now.strftime(format='%d')}"

        os.makedirs(name=log_dir, 
                    exist_ok=True)

        try:
            with open(file=f"{log_dir}/{now.strftime(format='%I:%M %p')}", 
                      mode="w") as file:
                file.write("\n".join(f"{now.strftime('%d-%m-%Y')} {now.strftime(format='%H:%M:%S')}:{str(round(number=int(now.strftime(format='%f'))/1000)).rjust(3, '0')} {log.log_level}  [site:{log.origin}]: {log.content}" for log in console.logs))

        except Exception as error:
            console.error(text=f"Failed to dump logs to file.\n\n{error}\n\n{error.__cause__}")

        # ====================
        # Print goodbye FIGlet
        # ====================
        print_figlet(text=config.figlet.exit, 
                     width=80, 
                     font=config.figlet.font)

        # ====================
        # Safely close program
        # ====================
        os._exit(status=2)

    def boot(self,
             host: typing.Optional[str] = "127.0.0.1",
             port: typing.Optional[int] = 5000,
             debug: typing.Optional[bool] = False):
        """Begins the process of starting and running the webserver.
        
        This function contains blocking code, so needs to be ran last."""
        
        # =====================
        # Register exit handler
        # =====================
        register(self.exit)

        # ============================
        # Prepare system for debugging
        # ============================
        if debug:
            console.log_level = "DEBUG"

            console.debug(text="Starting in Debug mode.")

        # =======================================
        # Configure HoneyBadger exception logging
        # =======================================
        if config.honeybadger.enabled:
            try:
                # ====================================
                # Flask - HoneyBadger config variables
                # ====================================
                app.config["HONEYBADGER_ENVIRONMENT"] = "development" if debug else "production"
                app.config["HONEYBADGER_API_KEY"] = config.honeybadger.key

                FlaskHoneybadger(app=app, 
                                 report_exceptions=True)

                console.info(text="HoneyBadger is now tracking errors.")

            except Exception as error:
                console.warn(text=f"Failed to setup HoneyBadger tracking.\n\n{error}\n\n{error.__cause__}")

        # ============================
        # Connect to PostgreSQL server
        # ============================
        try:
            constants.pool = psycopg2.connect(dsn="postgresql://{pg.user}:{pg.password}@{pg.host}:{pg.port}/{pg.database}".format(pg=config.postgres))
            constants.pool.set_session(autocommit=True)
            
            console.info(text="Connected to PostgreSQL database at: {pg.user}@{pg.host}/{pg.database}".format(pg=config.postgres))

        except Exception as error:
            console.fatal(text="Failed to connect to PostgreSQL database at: {pg.user}@{pg.host}/{pg.database}\n\n{error}\n\n{cause}".format(pg=config.postgres,
                                                                                                                                             error=error,
                                                                                                                                             cause=error.__cause__))
            
            self.exit()

        with constants.pool.cursor() as con:
            # ================================
            # Ensure dependant db tables exist
            # ================================
            queries = ("""CREATE TABLE IF NOT EXISTS image_users (id SERIAL PRIMARY KEY, username TEXT UNIQUE, password TEXT, display_name TEXT, admin BOOLEAN, created_at TIMESTAMP, api_token TEXT);""",
                       """CREATE TABLE IF NOT EXISTS uploaded_images (id SERIAL PRIMARY KEY, owner_id INT, discriminator TEXT UNIQUE, created_at TIMESTAMP);""")

            for query in queries:
                con.execute(query)

            # =========================
            # Fill user and image cache
            # =========================
            query = """SELECT id, username, password, display_name, admin, created_at, api_token
                       FROM image_users
                       ORDER BY id ASC;"""

            con.execute(query)

            for account in con.fetchall():
                cache.users.append(user(id=account[0],
                                        username=account[1],
                                        password=account[2],
                                        display_name=account[3],
                                        admin=account[4],
                                        created_at=account[5],
                                        token=account[6]))

            query = """SELECT id, owner_id, discriminator, created_at
                       FROM uploaded_images
                       ORDER BY created_at DESC;"""

            con.execute(query)

            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            # this import is here because utilities will import pool from constants,
            # and since importing it at the start means that pool isn't defined, it will error
            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            from imagoweb.util.utilities import first

            for image in con.fetchall():
                cache.images.append(upload(id=image[0],
                                           owner_id=image[1],
                                           discrim=image[2],
                                           created_at=image[3],
                                           owner=first(iterable=cache.users,
                                                       condition=lambda user: user.user_id == image[1])))

        console.info(text=f"Server started at: http://{host}:{port}")

        # ==================
        # Initialise plugins
        # ==================
        for plugin in (file[:-3] for file in os.listdir(path="plugins") if file.endswith(".py")):
            try:
                __import__(name=f"plugins.{plugin}")
                console.debug(text=f"{plugin} plugin successfully booted.")

            except Exception as error:
                console.error(text=f"{plugin} plugin failed to boot.\n\n{error}\n\n{error.__cause__}")

                self.exit()

        # =============================
        # Start server and print FIGlet
        # =============================
        print_figlet(text=config.figlet.boot,
                     width=80,
                     font=config.figlet.font)

        if debug:
            try:
                app.run(host=host,
                        port=port)

            except RuntimeError:
                self.exit()

if __name__ == "__main__":
    # ===============
    # Clean up stdout
    # ===============
    os.system(command="clear")

    # ===================
    # Initialise Imago
    # ===================
    if "debug" in sys.argv:
        Imago().boot(debug=True)

    else:
        Imago().boot(host=config.server.host, 
                     port=config.server.port,
                     debug=False)

        WSGIServer((config.server.host, config.server.port), app, 
                   log=None).serve_forever()