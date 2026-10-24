/**
@file   ModuleRnsPartyline.h
@brief  Puente de control y audio entre SvxLink y un sidecar Partyline (RNS/LXST)
*/

#ifndef MODULE_RNS_PARTYLINE_INCLUDED
#define MODULE_RNS_PARTYLINE_INCLUDED

#include <atomic>
#include <chrono>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <AsyncTimer.h>
#include <Module.h>
#include <version/SVXLINK.h>

class ModuleRnsPartyline : public Module
{
  public:
    ModuleRnsPartyline(void *dl_handle, Logic *logic,
                       const std::string& cfg_name);
    ~ModuleRnsPartyline(void) override;

    const char *compiledForVersion(void) const override
    {
      return SVXLINK_APP_VERSION;
    }

  private:
    bool initialize(void) override;
    int writeSamples(const float *samples, int count) override;
    void flushSamples(void) override;
    void allSamplesFlushed(void) override;
    void resumeOutput(void) override;
    void activateInit(void) override;
    void deactivateCleanup(void) override;
    void dtmfCmdReceived(const std::string& cmd) override;
    void squelchOpen(bool is_open) override;

    void connectionLoop(void);
    void readLoop(int fd);
    void sendHello(void);
    void sendActivation(bool active);
    void sendPtt(bool on);
    bool sendLine(const std::string& line);
    void logLine(const std::string& line);
    void waitForStop(int milliseconds);
    void shutdownSocket(void);

    void audioConnectionLoop(void);
    void audioReadLoop(int fd);
    bool parseAudioFrames(void);
    bool flushAudioTx(int fd);
    void pushRxSamples(const int16_t *samples, size_t count);
    void clearRx(void);
    void clearTx(void);
    void clearAudioRings(void);
    void startAudio(void);
    void stopAudio(void);
    void shutdownAudio(void);
    void waitForAudioStop(int milliseconds);
    void onActivationTimer(Async::Timer *timer);
    void onRxTimer(Async::Timer *timer);
    void pushRxToSink(void);
    void logAudioError(const std::string& text);

    std::string socket_path_;
    std::string audio_socket_path_;
    int reconnect_delay_ms_;
    int audio_rate_;
    int internal_rate_;
    int resample_ratio_;
    int audio_push_ms_;
    bool auto_connect_;
    bool text_only_;
    bool auto_activate_;
    bool audio_mode_;
    bool audio_ok_;
    size_t audio_chunk_samples_;
    size_t internal_chunk_samples_;

    std::atomic<bool> stop_;
    std::atomic<bool> active_;
    std::thread thread_;
    std::mutex fd_mutex_;
    int fd_;
    std::mutex send_mutex_;

    std::atomic<bool> audio_stop_;
    std::thread audio_thread_;
    std::mutex audio_fd_mutex_;
    int audio_fd_;
    std::atomic<bool> audio_connected_;
    std::atomic<bool> rx_ready_;
    std::atomic<bool> squelch_open_;
    std::atomic<bool> ptt_state_;
    bool ptt_logged_;
    bool rx_has_audio_;
    std::chrono::steady_clock::time_point last_rx_audio_;

    std::mutex tx_mutex_;
    std::deque<float> tx_ring_;
    std::mutex rx_mutex_;
    std::deque<float> rx_ring_;
    std::vector<uint8_t> audio_pending_;
    std::vector<uint8_t> tx_pending_;
    size_t tx_pending_offset_;
    uint64_t tx_dropped_;
    uint64_t rx_dropped_;

    static constexpr int RX_IDLE_FLUSH_MS = 200;

    Async::Timer activation_timer_;
    Async::Timer rx_timer_;
};

#endif /* MODULE_RNS_PARTYLINE_INCLUDED */
