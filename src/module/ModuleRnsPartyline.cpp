/**
@file   ModuleRnsPartyline.cpp
@brief  Puente de control y audio entre SvxLink y un sidecar Partyline (RNS/LXST)
*/

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <sstream>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <AsyncConfig.h>
#include <AsyncTimer.h>

#include "ModuleRnsPartyline.h"

using namespace std;

namespace {

constexpr uint8_t AUDIO_TYPE_PCM = 0x01;
constexpr size_t AUDIO_HEADER_SIZE = 5;
constexpr uint32_t MAX_AUDIO_FRAME = 65536;
constexpr size_t MAX_AUDIO_PENDING = 262144;

bool parseBool(const string& value, bool default_value)
{
  string text = value;
  transform(text.begin(), text.end(), text.begin(), ::tolower);
  if (text == "1" || text == "true" || text == "yes" || text == "on" ||
      text == "si" || text == "sí")
  {
    return true;
  }
  if (text == "0" || text == "false" || text == "no" || text == "off")
  {
    return false;
  }
  return default_value;
}

} // namespace


extern "C" {
  Module *module_init(void *dl_handle, Logic *logic, const char *cfg_name)
  {
    return new ModuleRnsPartyline(dl_handle, logic, cfg_name);
  }
} /* extern "C" */


ModuleRnsPartyline::ModuleRnsPartyline(void *dl_handle, Logic *logic,
                                       const string& cfg_name)
  : Module(dl_handle, logic, cfg_name),
    reconnect_delay_ms_(5000),
    audio_rate_(48000),
    internal_rate_(16000),
    resample_ratio_(3),
    audio_push_ms_(20),
    auto_connect_(true),
    text_only_(true),
    auto_activate_(true),
    audio_mode_(false),
    audio_ok_(false),
    audio_chunk_samples_(960),
    internal_chunk_samples_(320),
    stop_(false),
    active_(false),
    fd_(-1),
    audio_stop_(false),
    audio_fd_(-1),
    audio_connected_(false),
    rx_ready_(false),
    squelch_open_(false),
    ptt_state_(false),
    ptt_logged_(false),
    rx_has_audio_(false),
    last_rx_audio_(std::chrono::steady_clock::now()),
    tx_pending_offset_(0),
    tx_dropped_(0),
    rx_dropped_(0),
    activation_timer_(100, Async::Timer::TYPE_ONESHOT, false),
    rx_timer_(20, Async::Timer::TYPE_PERIODIC, false)
{
  activation_timer_.expired.connect(
      sigc::mem_fun(*this, &ModuleRnsPartyline::onActivationTimer));
  rx_timer_.expired.connect(
      sigc::mem_fun(*this, &ModuleRnsPartyline::onRxTimer));
} /* ModuleRnsPartyline */


ModuleRnsPartyline::~ModuleRnsPartyline(void)
{
  activation_timer_.setEnable(false);
  rx_timer_.setEnable(false);
  sendPtt(false);
  stop_ = true;
  audio_stop_ = true;
  shutdownSocket();
  shutdownAudio();
  if (thread_.joinable())
  {
    thread_.join();
  }
  if (audio_thread_.joinable())
  {
    audio_thread_.join();
  }
} /* ~ModuleRnsPartyline */


bool ModuleRnsPartyline::initialize(void)
{
  if (!Module::initialize())
  {
    return false;
  }

  if (!cfg().getValue(cfgName(), "SOCKET_PATH", socket_path_) &&
      !cfg().getValue("bridge", "SOCKET_PATH", socket_path_))
  {
    cerr << "*** ERROR: Config variable " << cfgName()
         << "/SOCKET_PATH or bridge/SOCKET_PATH not set" << endl;
    return false;
  }
  if (socket_path_.empty())
  {
    cerr << "*** ERROR: Empty SOCKET_PATH for module " << cfgName() << endl;
    return false;
  }

  cfg().getValue("bridge", "RECONNECT_DELAY_MS", reconnect_delay_ms_);
  if (reconnect_delay_ms_ < 100)
  {
    reconnect_delay_ms_ = 100;
  }

  int auto_connect = 1;
  if (cfg().getValue(cfgName(), "AUTO_CONNECT", auto_connect))
  {
    auto_connect_ = (auto_connect != 0);
  }

  string auto_activate = "1";
  if (cfg().getValue(cfgName(), "AUTO_ACTIVATE", auto_activate))
  {
    auto_activate_ = parseBool(auto_activate, true);
  }

  string text_only = "1";
  if (cfg().getValue("partyline", "TEXT_ONLY", text_only))
  {
    text_only_ = parseBool(text_only, true);
  }
  else
  {
    text_only_ = true;
  }

  if (!cfg().getValue("bridge", "AUDIO_SOCKET_PATH", audio_socket_path_) ||
      audio_socket_path_.empty())
  {
    audio_socket_path_ = socket_path_ + ".audio";
  }

  cfg().getValue("partyline", "SAMPLE_RATE", audio_rate_);
  cfg().getValue("bridge", "AUDIO_PUSH_MS", audio_push_ms_);
  if (audio_push_ms_ < 1)
  {
    audio_push_ms_ = 20;
  }
  if (audio_push_ms_ > 200)
  {
    audio_push_ms_ = 200;
  }
  audio_chunk_samples_ = max<size_t>(
      1, static_cast<size_t>(audio_rate_) * audio_push_ms_ / 1000);

  internal_rate_ = INTERNAL_SAMPLE_RATE;
  if (internal_rate_ <= 0 || audio_rate_ <= 0 ||
      (audio_rate_ % internal_rate_) != 0)
  {
    cerr << "*** ERROR: Module RnsPartyline: no se puede convertir "
         << internal_rate_ << " Hz internos a " << audio_rate_
         << " Hz de audio" << endl;
    audio_mode_ = false;
    resample_ratio_ = 1;
  }
  else
  {
    resample_ratio_ = audio_rate_ / internal_rate_;
  }
  internal_chunk_samples_ = max<size_t>(
      1, audio_chunk_samples_ / static_cast<size_t>(resample_ratio_));

  audio_mode_ = !text_only_ && (audio_rate_ == 48000);
  audio_ok_ = audio_mode_;

  if (text_only_)
  {
    audio_mode_ = false;
  }
  else if (audio_rate_ != 48000)
  {
    cerr << "*** ERROR: Module RnsPartyline: CARD_SAMPLE_RATE="
         << audio_rate_ << " no soportado en audio; se exige 48000" << endl;
    audio_mode_ = false;
  }

  if (audio_mode_)
  {
    if (audio_socket_path_.empty() || audio_socket_path_[0] != '/')
    {
      cerr << "*** ERROR: Module RnsPartyline: AUDIO_SOCKET_PATH debe ser "
              "absoluto ("
           << audio_socket_path_ << ")" << endl;
      return false;
    }
    if (audio_socket_path_ == socket_path_)
    {
      cerr << "*** ERROR: Module RnsPartyline: AUDIO_SOCKET_PATH debe ser "
              "distinto de SOCKET_PATH" << endl;
      return false;
    }
  }

  cout << "\tModule RnsPartyline v" << SVXLINK_APP_VERSION
       << " starting (socket " << socket_path_ << ", audio "
       << (audio_mode_ ? audio_socket_path_ : "(deshabilitado)")
       << ", mode " << (audio_mode_ ? "audio" : "control") << ")" << endl;

  if (!auto_connect_)
  {
    cerr << "Module RnsPartyline: AUTO_CONNECT=0, no se inicia la conexion"
         << endl;
  }
  else
  {
    stop_ = false;
    thread_ = thread(&ModuleRnsPartyline::connectionLoop, this);
  }

  if (audio_mode_ && auto_activate_)
  {
    activation_timer_.setTimeout(100);
    activation_timer_.setEnable(true);
  }

  return true;
} /* ModuleRnsPartyline::initialize */


void ModuleRnsPartyline::connectionLoop(void)
{
  bool failure_logged = false;

  while (!stop_)
  {
    int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
    {
      if (!failure_logged)
      {
        cerr << "*** ERROR: Module RnsPartyline: socket(): "
             << strerror(errno) << endl;
        failure_logged = true;
      }
      waitForStop(reconnect_delay_ms_);
      continue;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", socket_path_.c_str());

    if (::connect(fd, reinterpret_cast<struct sockaddr*>(&addr),
                  sizeof(addr)) != 0)
    {
      if (!failure_logged)
      {
        cerr << "Module RnsPartyline: no se pudo conectar a " << socket_path_
             << ": " << strerror(errno) << endl;
        failure_logged = true;
      }
      ::close(fd);
      waitForStop(reconnect_delay_ms_);
      continue;
    }

    failure_logged = false;
    ptt_logged_ = false;

    {
      lock_guard<mutex> lock(fd_mutex_);
      fd_ = fd;
    }

    cerr << "Module RnsPartyline: conectado al sidecar " << socket_path_ << endl;
    sendHello();
    readLoop(fd);

    {
      lock_guard<mutex> lock(fd_mutex_);
      if (fd_ == fd)
      {
        fd_ = -1;
      }
    }
    ::close(fd);

    if (!stop_)
    {
      cerr << "Module RnsPartyline: sidecar desconectado; reintentando"
           << endl;
      waitForStop(reconnect_delay_ms_);
    }
  }
} /* ModuleRnsPartyline::connectionLoop */


void ModuleRnsPartyline::readLoop(int fd)
{
  string pending;
  char buffer[4096];
  struct pollfd pfd;
  pfd.fd = fd;
  pfd.events = POLLIN;

  while (!stop_)
  {
    pfd.revents = 0;
    int rc = ::poll(&pfd, 1, 200);
    if (rc < 0)
    {
      if (errno == EINTR)
      {
        continue;
      }
      cerr << "*** ERROR: Module RnsPartyline: poll(): "
           << strerror(errno) << endl;
      break;
    }
    if (rc == 0)
    {
      continue;
    }
    if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))
    {
      break;
    }
    if (!(pfd.revents & POLLIN))
    {
      continue;
    }

    ssize_t n = ::recv(fd, buffer, sizeof(buffer), 0);
    if (n == 0)
    {
      break;
    }
    if (n < 0)
    {
      if (errno == EINTR)
      {
        continue;
      }
      cerr << "*** ERROR: Module RnsPartyline: recv(): "
           << strerror(errno) << endl;
      break;
    }

    pending.append(buffer, static_cast<size_t>(n));

    size_t pos;
    while ((pos = pending.find('\n')) != string::npos)
    {
      string line = pending.substr(0, pos);
      pending.erase(0, pos + 1);
      if (!line.empty() && line.back() == '\r')
      {
        line.pop_back();
      }
      if (!line.empty())
      {
        logLine(line);
      }
    }

    if (pending.size() > 65536)
    {
      pending.clear();
    }
  }
} /* ModuleRnsPartyline::readLoop */


void ModuleRnsPartyline::sendHello(void)
{
  ostringstream hello;
  hello << "{\"cmd\":\"hello\",\"client\":\"svxlink\",\"ver\":\""
        << SVXLINK_APP_VERSION << "\",\"audio_rate\":" << audio_rate_
        << ",\"format\":\"s16le\",\"channels\":1,\"text_only\":"
        << (text_only_ ? 1 : 0) << ",\"active\":" << (active_ ? 1 : 0)
        << ",\"activation_gate\":1}\n";
  sendLine(hello.str());
} /* ModuleRnsPartyline::sendHello */


void ModuleRnsPartyline::sendActivation(bool active)
{
  if (!audio_mode_)
  {
    return;
  }
  const string command = active ? "{\"cmd\":\"activate\"}\n"
                                : "{\"cmd\":\"deactivate\"}\n";
  sendLine(command);
} /* ModuleRnsPartyline::sendActivation */


void ModuleRnsPartyline::sendPtt(bool on)
{
  if (!audio_mode_)
  {
    return;
  }
  ptt_state_ = on;
  ostringstream command;
  command << "{\"cmd\":\"ptt\",\"on\":" << (on ? "true" : "false") << "}\n";
  if (sendLine(command.str()))
  {
    ptt_logged_ = false;
  }
  else if (!ptt_logged_)
  {
    cerr << "Module RnsPartyline: PTT " << (on ? "on" : "off")
         << " sin sidecar conectado" << endl;
    ptt_logged_ = true;
  }
} /* ModuleRnsPartyline::sendPtt */


bool ModuleRnsPartyline::sendLine(const string& line)
{
  lock_guard<mutex> send_lock(send_mutex_);
  int fd;
  {
    lock_guard<mutex> fd_lock(fd_mutex_);
    fd = fd_;
  }
  if (fd < 0)
  {
    return false;
  }

  size_t offset = 0;
  while (offset < line.size())
  {
    ssize_t n = ::send(fd, line.data() + offset, line.size() - offset,
                       MSG_NOSIGNAL);
    if (n < 0)
    {
      if (errno == EINTR)
      {
        continue;
      }
      return false;
    }
    if (n == 0)
    {
      return false;
    }
    offset += static_cast<size_t>(n);
  }
  return true;
} /* ModuleRnsPartyline::sendLine */


void ModuleRnsPartyline::logLine(const string& line)
{
  cerr << "Module RnsPartyline: " << line << endl;
} /* ModuleRnsPartyline::logLine */


void ModuleRnsPartyline::waitForStop(int milliseconds)
{
  int remaining = milliseconds;
  while (remaining > 0 && !stop_)
  {
    int chunk = min(remaining, 100);
    this_thread::sleep_for(chrono::milliseconds(chunk));
    remaining -= chunk;
  }
} /* ModuleRnsPartyline::waitForStop */


void ModuleRnsPartyline::shutdownSocket(void)
{
  lock_guard<mutex> lock(fd_mutex_);
  if (fd_ >= 0)
  {
    ::shutdown(fd_, SHUT_RDWR);
  }
} /* ModuleRnsPartyline::shutdownSocket */


void ModuleRnsPartyline::audioConnectionLoop(void)
{
  bool failure_logged = false;

  while (!audio_stop_)
  {
    int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
    {
      if (!failure_logged)
      {
        logAudioError(string("socket(): ") + strerror(errno));
        failure_logged = true;
      }
      waitForAudioStop(reconnect_delay_ms_);
      continue;
    }

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s",
             audio_socket_path_.c_str());

    if (::connect(fd, reinterpret_cast<struct sockaddr*>(&addr),
                  sizeof(addr)) != 0)
    {
      if (!failure_logged)
      {
        logAudioError("no se pudo conectar a " + audio_socket_path_ + ": " +
                      strerror(errno));
        failure_logged = true;
      }
      ::close(fd);
      waitForAudioStop(reconnect_delay_ms_);
      continue;
    }

    int flags = ::fcntl(fd, F_GETFL, 0);
    if (flags >= 0)
    {
      ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }

    failure_logged = false;
    {
      lock_guard<mutex> lock(audio_fd_mutex_);
      audio_fd_ = fd;
    }
    audio_connected_ = true;
    audio_pending_.clear();
    clearAudioRings();
    cerr << "Module RnsPartyline: audio conectado a " << audio_socket_path_
         << endl;

    audioReadLoop(fd);

    audio_connected_ = false;
    {
      lock_guard<mutex> lock(audio_fd_mutex_);
      if (audio_fd_ == fd)
      {
        audio_fd_ = -1;
      }
    }
    ::close(fd);

    if (!audio_stop_)
    {
      cerr << "Module RnsPartyline: audio desconectado; reintentando" << endl;
      waitForAudioStop(reconnect_delay_ms_);
    }
  }
} /* ModuleRnsPartyline::audioConnectionLoop */


void ModuleRnsPartyline::audioReadLoop(int fd)
{
  char buffer[8192];

  while (!audio_stop_)
  {
    struct pollfd pfd;
    pfd.fd = fd;
    pfd.events = POLLIN;
    pfd.revents = 0;

    int rc = ::poll(&pfd, 1, 10);
    if (rc < 0)
    {
      if (errno == EINTR)
      {
        continue;
      }
      logAudioError(string("poll(): ") + strerror(errno));
      break;
    }
    if (rc > 0 && (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)))
    {
      break;
    }

    bool connection_ok = true;
    if (rc > 0 && (pfd.revents & POLLIN))
    {
      while (true)
      {
        ssize_t n = ::recv(fd, buffer, sizeof(buffer), 0);
        if (n > 0)
        {
          if (audio_pending_.size() + static_cast<size_t>(n) >
              MAX_AUDIO_PENDING)
          {
            logAudioError("buffer de recepcion de audio desbordado");
            connection_ok = false;
            break;
          }
          audio_pending_.insert(
              audio_pending_.end(),
              reinterpret_cast<const uint8_t*>(buffer),
              reinterpret_cast<const uint8_t*>(buffer) + n);
          if (!parseAudioFrames())
          {
            connection_ok = false;
            break;
          }
        }
        else if (n == 0)
        {
          connection_ok = false;
          break;
        }
        else if (errno == EINTR)
        {
          continue;
        }
        else if (errno == EAGAIN || errno == EWOULDBLOCK)
        {
          break;
        }
        else
        {
          logAudioError(string("recv(): ") + strerror(errno));
          connection_ok = false;
          break;
        }
      }
    }

    if (!connection_ok)
    {
      break;
    }
    if (!flushAudioTx(fd))
    {
      break;
    }
  }
} /* ModuleRnsPartyline::audioReadLoop */


bool ModuleRnsPartyline::parseAudioFrames(void)
{
  while (audio_pending_.size() >= AUDIO_HEADER_SIZE)
  {
    const uint8_t type = audio_pending_[0];
    const uint32_t length =
        static_cast<uint32_t>(audio_pending_[1]) |
        (static_cast<uint32_t>(audio_pending_[2]) << 8) |
        (static_cast<uint32_t>(audio_pending_[3]) << 16) |
        (static_cast<uint32_t>(audio_pending_[4]) << 24);

    if (length > MAX_AUDIO_FRAME)
    {
      logAudioError("frame de audio demasiado grande");
      return false;
    }
    if (audio_pending_.size() < AUDIO_HEADER_SIZE + length)
    {
      break;
    }

    if (type == AUDIO_TYPE_PCM)
    {
      if ((length % 2) != 0)
      {
        logAudioError("frame PCM con longitud impar");
        return false;
      }
      vector<int16_t> samples(length / 2);
      if (!samples.empty())
      {
        memcpy(samples.data(), audio_pending_.data() + AUDIO_HEADER_SIZE,
               length);
        pushRxSamples(samples.data(), samples.size());
      }
    }
    else
    {
      cerr << "Module RnsPartyline: frame de audio desconocido 0x"
           << hex << static_cast<int>(type) << dec << endl;
    }

    audio_pending_.erase(audio_pending_.begin(),
                         audio_pending_.begin() + AUDIO_HEADER_SIZE + length);
  }
  return true;
} /* ModuleRnsPartyline::parseAudioFrames */


bool ModuleRnsPartyline::flushAudioTx(int fd)
{
  if (tx_pending_.empty())
  {
    vector<float> block;
    {
      lock_guard<mutex> lock(tx_mutex_);
      if (tx_ring_.empty())
      {
        return true;
      }
      const size_t count = min(audio_chunk_samples_, tx_ring_.size());
      block.assign(tx_ring_.begin(), tx_ring_.begin() + count);
      tx_ring_.erase(tx_ring_.begin(), tx_ring_.begin() + count);
    }

    tx_pending_.resize(AUDIO_HEADER_SIZE + block.size() * 2);
    tx_pending_[0] = AUDIO_TYPE_PCM;
    const uint32_t length = static_cast<uint32_t>(block.size() * 2);
    tx_pending_[1] = length & 0xff;
    tx_pending_[2] = (length >> 8) & 0xff;
    tx_pending_[3] = (length >> 16) & 0xff;
    tx_pending_[4] = (length >> 24) & 0xff;
    for (size_t i = 0; i < block.size(); ++i)
    {
      const float clamped = max(-1.0f, min(1.0f, block[i]));
      const int16_t sample =
          static_cast<int16_t>(clamped * 32767.0f);
      memcpy(tx_pending_.data() + AUDIO_HEADER_SIZE + i * 2, &sample, 2);
    }
    tx_pending_offset_ = 0;
  }

  while (tx_pending_offset_ < tx_pending_.size())
  {
    ssize_t n = ::send(fd, tx_pending_.data() + tx_pending_offset_,
                       tx_pending_.size() - tx_pending_offset_,
                       MSG_NOSIGNAL);
    if (n > 0)
    {
      tx_pending_offset_ += static_cast<size_t>(n);
    }
    else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
    {
      return true;
    }
    else if (n < 0 && errno == EINTR)
    {
      continue;
    }
    else
    {
      logAudioError(string("send(): ") + strerror(errno));
      return false;
    }
  }

  tx_pending_.clear();
  tx_pending_offset_ = 0;
  return true;
} /* ModuleRnsPartyline::flushAudioTx */


void ModuleRnsPartyline::pushRxSamples(const int16_t *samples, size_t count)
{
  if (samples == nullptr || count == 0)
  {
    return;
  }
  const size_t max_samples = static_cast<size_t>(audio_rate_) * 2;
  lock_guard<mutex> lock(rx_mutex_);
  for (size_t i = 0; i < count; ++i)
  {
    rx_ring_.push_back(static_cast<float>(samples[i]) / 32768.0f);
    if (rx_ring_.size() > max_samples)
    {
      rx_ring_.pop_front();
      ++rx_dropped_;
      if ((rx_dropped_ % 48000) == 1)
      {
        logAudioError("anillo RX desbordado; descartando muestras antiguas");
      }
    }
  }
} /* ModuleRnsPartyline::pushRxSamples */


void ModuleRnsPartyline::clearRx(void)
{
  lock_guard<mutex> lock(rx_mutex_);
  rx_ring_.clear();
} /* ModuleRnsPartyline::clearRx */


void ModuleRnsPartyline::clearTx(void)
{
  lock_guard<mutex> lock(tx_mutex_);
  tx_ring_.clear();
  tx_pending_.clear();
  tx_pending_offset_ = 0;
} /* ModuleRnsPartyline::clearTx */


void ModuleRnsPartyline::clearAudioRings(void)
{
  clearTx();
  clearRx();
  audio_pending_.clear();
} /* ModuleRnsPartyline::clearAudioRings */


void ModuleRnsPartyline::startAudio(void)
{
  if (!audio_mode_ || audio_thread_.joinable())
  {
    return;
  }
  audio_stop_ = false;
  audio_thread_ = thread(&ModuleRnsPartyline::audioConnectionLoop, this);
} /* ModuleRnsPartyline::startAudio */


void ModuleRnsPartyline::stopAudio(void)
{
  if (!audio_thread_.joinable())
  {
    return;
  }
  audio_stop_ = true;
  shutdownAudio();
  audio_thread_.join();
  audio_thread_ = thread();
} /* ModuleRnsPartyline::stopAudio */


void ModuleRnsPartyline::shutdownAudio(void)
{
  lock_guard<mutex> lock(audio_fd_mutex_);
  if (audio_fd_ >= 0)
  {
    ::shutdown(audio_fd_, SHUT_RDWR);
  }
} /* ModuleRnsPartyline::shutdownAudio */


void ModuleRnsPartyline::waitForAudioStop(int milliseconds)
{
  int remaining = milliseconds;
  while (remaining > 0 && !audio_stop_)
  {
    int chunk = min(remaining, 100);
    this_thread::sleep_for(chrono::milliseconds(chunk));
    remaining -= chunk;
  }
} /* ModuleRnsPartyline::waitForAudioStop */


void ModuleRnsPartyline::onActivationTimer(Async::Timer *timer)
{
  (void)timer;
  if (!audio_mode_ || isActive())
  {
    return;
  }
  if (!activateMe())
  {
    cerr << "Module RnsPartyline: no se pudo activar (otro modulo activo?); "
            "reintentando"
         << endl;
    activation_timer_.setTimeout(2000);
    activation_timer_.setEnable(true);
  }
} /* ModuleRnsPartyline::onActivationTimer */


void ModuleRnsPartyline::onRxTimer(Async::Timer *timer)
{
  (void)timer;
  pushRxToSink();
} /* ModuleRnsPartyline::onRxTimer */


void ModuleRnsPartyline::pushRxToSink(void)
{
  if (!audio_mode_ || !isActive() || !rx_ready_)
  {
    return;
  }

  const auto now = std::chrono::steady_clock::now();

  if (squelch_open_)
  {
    clearRx();
    if (rx_has_audio_)
    {
      rx_has_audio_ = false;
      sinkFlushSamples();
    }
    return;
  }

  vector<float> wire_block;
  {
    lock_guard<mutex> lock(rx_mutex_);
    if (rx_ring_.empty())
    {
      if (rx_has_audio_ &&
          now - last_rx_audio_ >= std::chrono::milliseconds(RX_IDLE_FLUSH_MS))
      {
        rx_has_audio_ = false;
        sinkFlushSamples();
      }
      return;
    }
    const size_t count = min(audio_chunk_samples_, rx_ring_.size());
    wire_block.assign(rx_ring_.begin(), rx_ring_.begin() + count);
    rx_ring_.erase(rx_ring_.begin(), rx_ring_.begin() + count);
  }

  vector<float> internal_block(internal_chunk_samples_);
  for (size_t i = 0; i < internal_chunk_samples_; ++i)
  {
    const size_t source = min(i * static_cast<size_t>(resample_ratio_),
                              wire_block.size() - 1);
    internal_block[i] = wire_block[source];
  }

  int written = sinkWriteSamples(internal_block.data(),
                                 static_cast<int>(internal_block.size()));
  if (written < 0)
  {
    written = 0;
  }
  if (written > 0)
  {
    rx_has_audio_ = true;
    last_rx_audio_ = now;
  }
  if (written < static_cast<int>(internal_block.size()))
  {
    const size_t consumed =
        static_cast<size_t>(written) * static_cast<size_t>(resample_ratio_);
    if (consumed < wire_block.size())
    {
      lock_guard<mutex> lock(rx_mutex_);
      rx_ring_.insert(rx_ring_.begin(), wire_block.begin() + consumed,
                      wire_block.end());
    }
    rx_ready_ = false;
  }
} /* ModuleRnsPartyline::pushRxToSink */


void ModuleRnsPartyline::logAudioError(const string& text)
{
  cerr << "*** ERROR: Module RnsPartyline audio: " << text << endl;
} /* ModuleRnsPartyline::logAudioError */


int ModuleRnsPartyline::writeSamples(const float *samples, int count)
{
  if (!audio_mode_ || !isActive() || samples == nullptr || count <= 0)
  {
    return count;
  }
  if (!audio_connected_)
  {
    return count;
  }

  const size_t max_samples = static_cast<size_t>(audio_rate_) * 2;
  lock_guard<mutex> lock(tx_mutex_);
  for (int i = 0; i < count; ++i)
  {
    for (int copy = 0; copy < resample_ratio_; ++copy)
    {
      tx_ring_.push_back(samples[i]);
      if (tx_ring_.size() > max_samples)
      {
        tx_ring_.pop_front();
        ++tx_dropped_;
      }
    }
  }
  return count;
} /* ModuleRnsPartyline::writeSamples */


void ModuleRnsPartyline::flushSamples(void)
{
  clearTx();
  sourceAllSamplesFlushed();
} /* ModuleRnsPartyline::flushSamples */


void ModuleRnsPartyline::allSamplesFlushed(void)
{
} /* ModuleRnsPartyline::allSamplesFlushed */


void ModuleRnsPartyline::resumeOutput(void)
{
  rx_ready_ = true;
} /* ModuleRnsPartyline::resumeOutput */


void ModuleRnsPartyline::activateInit(void)
{
  if (audio_mode_)
  {
    active_ = true;
    sendActivation(true);
    rx_has_audio_ = false;
    startAudio();
    rx_ready_ = true;
    rx_timer_.setEnable(true);
  }
} /* ModuleRnsPartyline::activateInit */


void ModuleRnsPartyline::dtmfCmdReceived(const std::string& cmd)
{
  if (cmd.empty())
  {
    deactivateMe();
  }
} /* ModuleRnsPartyline::dtmfCmdReceived */


void ModuleRnsPartyline::deactivateCleanup(void)
{
  active_ = false;
  sendActivation(false);
  activation_timer_.setEnable(false);
  rx_timer_.setEnable(false);
  squelch_open_ = false;
  sendPtt(false);
  stopAudio();
  clearAudioRings();
  rx_ready_ = false;
  rx_has_audio_ = false;
} /* ModuleRnsPartyline::deactivateCleanup */


void ModuleRnsPartyline::squelchOpen(bool is_open)
{
  if (!audio_mode_)
  {
    return;
  }
  squelch_open_ = is_open;
  if (is_open)
  {
    clearRx();
  }
  sendPtt(is_open);
} /* ModuleRnsPartyline::squelchOpen */
