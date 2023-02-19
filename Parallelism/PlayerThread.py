import asyncio
from time import time
from urllib.parse import parse_qs, urlparse
from discord import VoiceClient
from asyncio import AbstractEventLoop
from threading import RLock, Thread
from multiprocessing import Lock
from typing import Callable
from discord import Guild, FFmpegPCMAudio, VoiceChannel
from Music.Playlist import Playlist
from Music.Song import Song
from Config.Configs import VConfigs
from Music.VulkanBot import VulkanBot
from Music.Downloader import Downloader
from Parallelism.Commands import VCommands, VCommandsType


class TimeoutClock:
    def __init__(self, callback: Callable, loop: asyncio.AbstractEventLoop):
        self.__callback = callback
        self.__task = loop.create_task(self.__executor())

    async def __executor(self):
        await asyncio.sleep(VConfigs().VC_TIMEOUT)
        await self.__callback()

    def cancel(self):
        self.__task.cancel()


class PlayerThread(Thread):
    """Player Thread to control the song playback in the same Process of the Main Process"""

    def __init__(self, bot: VulkanBot, guild: Guild, name: str, voiceChannel: VoiceChannel, playlist: Playlist, lock: Lock, guildID: int, voiceID: int) -> None:
        Thread.__init__(self, name=name, group=None, target=None, args=(), kwargs={})
        # Synchronization objects
        self.__playlist: Playlist = playlist
        self.__playlistLock: Lock = lock
        self.__loop: AbstractEventLoop = None
        self.__playerLock: RLock = RLock()
        # Discord context ID
        self.__guildID = guildID
        self.__voiceChannelID = voiceID
        self.__guild: Guild = guild
        self.__bot: VulkanBot = bot
        self.__voiceChannel: VoiceChannel = voiceChannel
        self.__voiceClient: VoiceClient = None

        self.__downloader = Downloader()

        self.__playing = False
        self.__forceStop = False
        self.FFMPEG_OPTIONS = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                               'options': '-vn'}

    def run(self) -> None:
        """This method is called automatically when the Thread starts"""
        try:
            print(f'Starting Player Thread for Guild {self.name}')
            self.__loop = asyncio.get_event_loop_policy().new_event_loop()
            asyncio.set_event_loop(self.__loop)
            self.__loop.run_until_complete(self._run())

        except Exception as e:
            print(f'[Error in Process {self.name}] -> {e}')

    async def _run(self) -> None:
        # Connect to voice Channel
        await self.__connectToVoiceChannel()
        # Start the timeout function
        self.__timer = TimeoutClock(self.__timeoutHandler, self.__loop)
        # Start a Task to play songs
        self.__loop.create_task(self.__playPlaylistSongs())

    def __verifyIfIsPlaying(self) -> bool:
        if self.__voiceClient is None:
            return False
        if not self.__voiceClient.is_connected():
            return False
        return self.__voiceClient.is_playing() or self.__voiceClient.is_paused()

    async def __playPlaylistSongs(self) -> None:
        """If the player is not running trigger to play a new song"""
        self.__playing = self.__verifyIfIsPlaying()
        if not self.__playing:
            song = None
            with self.__playlistLock:
                with self.__playerLock:
                    song = self.__playlist.next_song()

            if song is not None:
                self.__loop.create_task(self.__playSong(song), name=f'Song {song.identifier}')
                self.__playing = True

    async def __playSong(self, song: Song) -> None:
        """Function that will trigger the player to play the song"""
        try:
            self.__playerLock.acquire()
            if song is None:
                return

            if song.source is None:
                return self.__playNext(None)

            # If not connected, connect to bind channel
            if self.__voiceClient is None:
                await self.__connectToVoiceChannel()

            # If the voice channel disconnect for some reason
            if not self.__voiceClient.is_connected():
                print('[VOICE CHANNEL NOT NULL BUT DISCONNECTED, CONNECTING AGAIN]')
                await self.__connectToVoiceChannel()
            # If the player is connected and playing return the song to the playlist
            elif self.__voiceClient.is_playing():
                print('[SONG ALREADY PLAYING, RETURNING]')
                self.__playlist.add_song_start(song)
                return

            songStillAvailable = self.__verifyIfSongAvailable(song)
            if not songStillAvailable:
                print('[SONG NOT AVAILABLE ANYMORE, DOWNLOADING AGAIN]')
                song = self.__downloadSongAgain(song)

            self.__playing = True
            self.__songPlaying = song

            player = FFmpegPCMAudio(song.source, **self.FFMPEG_OPTIONS)
            self.__voiceClient.play(player, after=lambda e: self.__playNext(e))

            self.__timer.cancel()
            self.__timer = TimeoutClock(self.__timeoutHandler, self.__loop)

            nowPlayingCommand = VCommands(VCommandsType.NOW_PLAYING, song)
            self.__queueSend.put(nowPlayingCommand)
        except Exception as e:
            print(f'[ERROR IN PLAY SONG FUNCTION] -> {e}, {type(e)}')
            self.__playNext(None)
        finally:
            self.__playerLock.release()

    def __playNext(self, error) -> None:
        if error is not None:
            print(f'[ERROR PLAYING SONG] -> {error}')
        with self.__playlistLock:
            with self.__playerLock:
                if self.__forceStop:  # If it's forced to stop player
                    self.__forceStop = False
                    return None

                song = self.__playlist.next_song()

                if song is not None:
                    self.__loop.create_task(self.__playSong(song), name=f'Song {song.identifier}')
                else:
                    self.__playlist.loop_off()
                    self.__songPlaying = None
                    self.__playing = False
                    # Send a command to the main process put this one to sleep
                    sleepCommand = VCommands(VCommandsType.SLEEPING)
                    self.__queueSend.put(sleepCommand)
                    # Release the semaphore to finish the process
                    self.__semStopPlaying.release()

    def __verifyIfSongAvailable(self, song: Song) -> bool:
        """Verify the song source to see if it's already expired"""
        try:
            parsedUrl = urlparse(song.source)

            if 'expire' not in parsedUrl.query:
                # If already passed 5 hours since the download
                if song.downloadTime + 18000 < int(time()):
                    return False
                return True

            # If the current time plus the song duration plus 10min exceeds the expirationValue
            expireValue = parse_qs(parsedUrl.query)['expire'][0]
            if int(time()) + song.duration + 600 > int(str(expireValue)):
                return False
            return True
        except Exception as e:
            print(f'[ERROR VERIFYING SONG AVAILABILITY] -> {e}')
            return False

    def __downloadSongAgain(self, song: Song) -> Song:
        """Force a download to be executed again, one use case is when the song.source expired and needs to refresh"""
        return self.__downloader.finish_one_song(song)

    async def __playPrev(self, voiceChannelID: int) -> None:
        with self.__playlistLock:
            song = self.__playlist.prev_song()

            with self.__playerLock:
                if song is not None:
                    # If not connect, connect to the user voice channel, may change the channel
                    if self.__voiceClient is None or not self.__voiceClient.is_connected():
                        self.__voiceChannelID = voiceChannelID
                        self.__voiceChannel = self.__guild.get_channel(self.__voiceChannelID)
                        await self.__connectToVoiceChannel()

                    # If already playing, stop the current play
                    if self.__verifyIfIsPlaying():
                        # Will forbidden next_song to execute after stopping current player
                        self.__forceStop = True
                        self.__voiceClient.stop()
                        self.__playing = False

                    self.__loop.create_task(self.__playSong(song), name=f'Song {song.identifier}')

    async def __restartCurrentSong(self) -> None:
        song = self.__playlist.getCurrentSong()
        if song is None:
            song = self.__playlist.next_song()
        if song is None:
            return

        self.__loop.create_task(self.__playSong(song), name=f'Song {song.identifier}')

    async def receiveCommand(self, command: VCommands) -> None:
        type = command.getType()
        args = command.getArgs()
        print(f'Player Thread {self.__guild.name} received command {type}')

        try:
            self.__playerLock.acquire()
            if type == VCommandsType.PAUSE:
                self.__pause()
            elif type == VCommandsType.RESUME:
                await self.__resume()
            elif type == VCommandsType.SKIP:
                await self.__skip()
            elif type == VCommandsType.PLAY:
                await self.__playPlaylistSongs()
            elif type == VCommandsType.PREV:
                await self.__playPrev(args)
            elif type == VCommandsType.RESET:
                await self.__reset()
            elif type == VCommandsType.STOP:
                await self.__stop()
            else:
                print(f'[ERROR] -> Unknown Command Received: {command}')
        except Exception as e:
            print(f'[ERROR IN COMMAND RECEIVER] -> {type} - {e}')
        finally:
            self.__playerLock.release()

    def __pause(self) -> None:
        if self.__voiceClient is not None:
            if self.__voiceClient.is_connected():
                if self.__voiceClient.is_playing():
                    self.__voiceClient.pause()

    async def __reset(self) -> None:
        if self.__voiceClient is None:
            return

        if not self.__voiceClient.is_connected():
            await self.__connectToVoiceChannel()
        if self.__songPlaying is not None:
            await self.__restartCurrentSong()

    async def __stop(self) -> None:
        if self.__voiceClient is not None:
            if self.__voiceClient.is_connected():
                with self.__playlistLock:
                    self.__playlist.loop_off()
                    self.__playlist.clear()

                self.__voiceClient.stop()
                await self.__voiceClient.disconnect()

                self.__songPlaying = None
                self.__playing = False
                self.__voiceClient = None
            # If the voiceClient is not None we finish things
            else:
                await self.__forceBotDisconnectAndStop()

    async def __resume(self) -> None:
        # Lock to work with Player
        with self.__playerLock:
            if self.__voiceClient is not None:
                # If the player is paused then return to play
                if self.__voiceClient.is_paused():
                    return self.__voiceClient.resume()
                # If there is a current song but the voice client is not playing
                elif self.__songPlaying is not None and not self.__voiceClient.is_playing():
                    await self.__playSong(self.__songPlaying)

    async def __skip(self) -> None:
        self.__playing = self.__verifyIfIsPlaying()
        # Lock to work with Player
        with self.__playerLock:
            if self.__playing:
                self.__playing = False
                self.__voiceClient.stop()
            # If for some reason the Bot has disconnect but there is still songs to play
            elif len(self.__playlist.getSongs()) > 0:
                print('[RESTARTING CURRENT SONG]')
                await self.__restartCurrentSong()

    async def __forceBotDisconnectAndStop(self) -> None:
        # Lock to work with Player
        with self.__playerLock:
            if self.__voiceClient is None:
                return
            self.__playing = False
            self.__songPlaying = None
            try:
                self.__voiceClient.stop()
                await self.__voiceClient.disconnect(force=True)
            except Exception as e:
                print(f'[ERROR FORCING BOT TO STOP] -> {e}')
            finally:
                self.__voiceClient = None
            with self.__playlistLock:
                self.__playlist.clear()
                self.__playlist.loop_off()

    async def __timeoutHandler(self) -> None:
        try:
            if self.__voiceClient is None:
                return

            # If the bot should not disconnect when alone
            if not VConfigs().SHOULD_AUTO_DISCONNECT_WHEN_ALONE:
                return

            if self.__voiceClient.is_connected():
                if self.__voiceClient.is_playing() or self.__voiceClient.is_paused():
                    if not self.__isBotAloneInChannel():  # If bot is not alone continue to play
                        self.__timer = TimeoutClock(self.__timeoutHandler, self.__loop)
                        return

            # Finish the process
            with self.__playerLock:
                with self.__playlistLock:
                    self.__playlist.loop_off()
                await self.__forceBotDisconnectAndStop()
        except Exception as e:
            print(f'[ERROR IN TIMEOUT] -> {e}')

    def __isBotAloneInChannel(self) -> bool:
        try:
            if len(self.__voiceClient.channel.members) <= 1:
                return True
            else:
                return False
        except Exception as e:
            print(f'[ERROR IN CHECK BOT ALONE] -> {e}')
            return False

    async def __connectToVoiceChannel(self) -> bool:
        try:
            print('[CONNECTING TO VOICE CHANNEL]')
            if self.__voiceClient is not None:
                try:
                    await self.__voiceClient.disconnect(force=True)
                except Exception as e:
                    print(f'[ERROR FORCING DISCONNECT] -> {e}')
            self.__voiceClient = await self.__voiceChannel.connect(reconnect=True, timeout=None)
            return True
        except Exception as e:
            print(f'[ERROR CONNECTING TO VC] -> {e}')
            return False
