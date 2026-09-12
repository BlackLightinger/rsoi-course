import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { beginLogin, finishLogin, logout, session, Session } from "./auth";

type Flight = {
  id: number; flight_number: string; origin_code: string; origin_city: string;
  destination_code: string; destination_city: string; departure_at: string;
  arrival_at: string; price_rub: number; seats_available: number; aircraft: string;
};
type Seat = { seat_number: string; status: "available" | "reserved" };
type BaggageType = "none" | "cabin" | "checked";
type Booking = {
  id: string; booking_code: string; flight_number: string; origin_code: string;
  destination_code: string; departure_at: string; passengers: number; amount_rub: number;
  status: string; passenger_name: string; seat_numbers: string; baggage_type: BaggageType;
  checked_in_at: string | null;
};
type Dashboard = {
  bookings: { items: Booking[]; count: number };
  loyalty: { points: number; tier: string } | null;
  payments: { items: unknown[]; count: number };
  warnings: string[];
};
type Report = {
  totals: { total_events: number; unique_subjects: number };
  breakdown: { service: string; action: string; event_count: number; error_count: number; error_rate: number }[];
};
type RegistrationForm = {
  username: string;
  full_name: string;
  email: string;
  password: string;
};
type PassengerForm = {
  name: string;
  email: string;
  phone: string;
  document: string;
  birthDate: string;
};

const rub = new Intl.NumberFormat("ru-RU", { style: "currency", currency: "RUB", maximumFractionDigits: 0 });
const time = (value: string) => new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
const clean = (value: string) => value.trim();
const baggageOptions: { value: BaggageType; title: string; hint: string; price: number }[] = [
  { value: "none", title: "Без багажа", hint: "Только личные вещи", price: 0 },
  { value: "cabin", title: "Ручная кладь", hint: "До 10 кг в салон", price: 0 },
  { value: "checked", title: "Багаж 23 кг", hint: "Чемодан в багажный отсек", price: 2500 }
];
const baggageLabel = (value: BaggageType) => baggageOptions.find(option => option.value === value)?.title || "Багаж";
const defaultPassenger = (name = ""): PassengerForm => ({ name, email: "", phone: "", document: "", birthDate: "" });

function App() {
  const [user, setUser] = useState<Session | null>(() => session());
  const [tab, setTab] = useState<"search" | "trips" | "admin">("search");
  const [flights, setFlights] = useState<Flight[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [originSuggestions, setOriginSuggestions] = useState<string[]>([]);
  const [destinationSuggestions, setDestinationSuggestions] = useState<string[]>([]);
  const [passengers, setPassengers] = useState(1);
  const [selected, setSelected] = useState<Flight | null>(null);
  const [passenger, setPassenger] = useState<PassengerForm>(() => defaultPassenger());
  const [seatMap, setSeatMap] = useState<Seat[]>([]);
  const [selectedSeats, setSelectedSeats] = useState<string[]>([]);
  const [baggageType, setBaggageType] = useState<BaggageType>("none");
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [registrationOpen, setRegistrationOpen] = useState(false);
  const [registration, setRegistration] = useState<RegistrationForm>({
    username: "",
    full_name: "",
    email: "",
    password: ""
  });

  useEffect(() => {
    if (window.location.pathname === "/callback") {
      setLoading(true);
      finishLogin().then(() => setUser(session())).catch(e => setMessage(e.message)).finally(() => setLoading(false));
      return;
    }
    if (new URLSearchParams(window.location.search).get("register") === "1") {
      setRegistrationOpen(true);
      window.history.replaceState({}, "", "/");
    }
  }, []);

  useEffect(() => {
    const q = clean(origin);
    const timer = window.setTimeout(async () => {
      if (!q) { setOriginSuggestions([]); return; }
      try {
        const result = await api<{ items: string[] }>(`/api/cities?${new URLSearchParams({ query: q })}`);
        setOriginSuggestions(result.items);
      } catch { setOriginSuggestions([]); }
    }, 180);
    return () => window.clearTimeout(timer);
  }, [origin]);

  useEffect(() => {
    const q = clean(destination);
    const timer = window.setTimeout(async () => {
      if (!q) { setDestinationSuggestions([]); return; }
      try {
        const result = await api<{ items: string[] }>(`/api/cities?${new URLSearchParams({ query: q })}`);
        setDestinationSuggestions(result.items);
      } catch { setDestinationSuggestions([]); }
    }, 180);
    return () => window.clearTimeout(timer);
  }, [destination]);

  useEffect(() => {
    setSelectedSeats(current => current.slice(0, passengers));
  }, [passengers]);

  const routeTitle = useMemo(() => {
    const from = clean(origin);
    const to = clean(destination);
    if (!from && !to) return "Все направления";
    return `${from || "…"} → ${to || "…"}`;
  }, [origin, destination]);

  const selectedBaggageFee = useMemo(() => {
    return (baggageOptions.find(option => option.value === baggageType)?.price || 0) * passengers;
  }, [baggageType, passengers]);

  async function search(e?: FormEvent) {
    e?.preventDefault();
    setLoading(true); setMessage("");
    try {
      const query = new URLSearchParams({ passengers: String(passengers) });
      if (clean(origin)) query.set("origin", clean(origin));
      if (clean(destination)) query.set("destination", clean(destination));
      const result = await api<{ items: Flight[] }>(`/api/flights?${query}`);
      setFlights(result.items);
      if (!result.items.length) setMessage("На этом направлении пока нет доступных мест.");
    } catch (e) { setMessage((e as Error).message); }
    finally { setLoading(false); }
  }

  useEffect(() => { search(); }, []);

  async function loadSeats(flightId: number) {
    try {
      const result = await api<{ items: Seat[] }>(`/api/flights/${flightId}/seats`);
      setSeatMap(result.items);
    } catch (e) {
      setSeatMap([]);
      setMessage((e as Error).message);
    }
  }

  async function openBooking(flight: Flight) {
    if (!user) { await beginLogin(); return; }
    setSelected(flight);
    setPassenger(defaultPassenger(user.name || ""));
    setSelectedSeats([]);
    setBaggageType("none");
    setSeatMap([]);
    await loadSeats(flight.id);
  }

  function toggleSeat(seat: Seat) {
    if (seat.status !== "available") return;
    setSelectedSeats(current => {
      if (current.includes(seat.seat_number)) return current.filter(item => item !== seat.seat_number);
      if (current.length >= passengers) {
        setMessage(`Для ${passengers} пасс. можно выбрать только ${passengers} мест.`);
        return current;
      }
      return [...current, seat.seat_number];
    });
  }

  async function book(e: FormEvent) {
    e.preventDefault();
    if (!user) { await beginLogin(); return; }
    if (!selected) return;
    if (selectedSeats.length !== passengers) {
      setMessage("Выберите места для всех пассажиров.");
      return;
    }
    setLoading(true); setMessage("");
    try {
      await api("/api/bookings", {
        method: "POST",
        body: JSON.stringify({
          flight_id: selected.id,
          passengers,
          passenger_name: clean(passenger.name),
          passenger_email: clean(passenger.email),
          passenger_phone: clean(passenger.phone),
          passenger_document: clean(passenger.document),
          passenger_birth_date: passenger.birthDate,
          seat_numbers: selectedSeats,
          baggage_type: baggageType
        })
      }, true);
      setSelected(null);
      setMessage("Места зарезервированы. Оплатите бронь в разделе «Мои поездки», затем пройдите онлайн-регистрацию.");
      await search();
      await loadDashboard();
    } catch (e) { setMessage((e as Error).message); }
    finally { setLoading(false); }
  }

  async function loadDashboard() {
    if (!user) return;
    setLoading(true); setMessage("");
    try { setDashboard(await api<Dashboard>("/api/dashboard", {}, true)); }
    catch (e) { setMessage((e as Error).message); }
    finally { setLoading(false); }
  }

  async function pay(booking: Booking) {
    setLoading(true); setMessage("");
    try {
      const result = await api<{ warnings: string[] }>(`/api/bookings/${booking.id}/pay`, {
        method: "POST", body: JSON.stringify({ method: "bank_card" })
      }, true);
      setMessage(result.warnings[0] || "Оплата прошла — теперь можно пройти онлайн-регистрацию.");
      await loadDashboard();
    } catch (e) { setMessage((e as Error).message); }
    finally { setLoading(false); }
  }

  async function checkIn(booking: Booking) {
    setLoading(true); setMessage("");
    try {
      await api(`/api/bookings/${booking.id}/check-in`, { method: "POST" }, true);
      setMessage(`Онлайн-регистрация на рейс ${booking.flight_number} пройдена.`);
      await loadDashboard();
    } catch (e) { setMessage((e as Error).message); }
    finally { setLoading(false); }
  }

  async function loadReport() {
    setLoading(true); setMessage("");
    try { setReport(await api<Report>("/api/admin/report?days=7", {}, true)); }
    catch (e) { setMessage((e as Error).message); }
    finally { setLoading(false); }
  }

  async function openTab(next: "search" | "trips" | "admin") {
    setTab(next);
    if (next === "trips") await loadDashboard();
    if (next === "admin") await loadReport();
  }

  async function register(e: FormEvent) {
    e.preventDefault();
    setLoading(true); setMessage("");
    try {
      await api<{ id: string; username: string }>("/api/register", {
        method: "POST",
        body: JSON.stringify({
          ...registration,
          username: clean(registration.username),
          full_name: clean(registration.full_name),
          email: clean(registration.email)
        })
      });
      setRegistrationOpen(false);
      setRegistration({ username: "", full_name: "", email: "", password: "" });
      setMessage("Аккаунт создан. Теперь войдите под новым логином и паролем.");
    } catch (e) { setMessage((e as Error).message); }
    finally { setLoading(false); }
  }

  async function downloadReport() {
    const token = sessionStorage.getItem("access_token");
    const response = await fetch("/api/admin/report.csv?days=7", { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) { setMessage("Не удалось скачать отчёт"); return; }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = "service-report.csv"; anchor.click();
    URL.revokeObjectURL(url);
  }

  return <>
    <header>
      <a className="brand" href="/" aria-label="AeroFlow"><span>✦</span> AeroFlow</a>
      <nav>
        <button className={tab === "search" ? "active" : ""} onClick={() => openTab("search")}>Поиск</button>
        {user && <button className={tab === "trips" ? "active" : ""} onClick={() => openTab("trips")}>Мои поездки</button>}
        {user?.role === "admin" && <button className={tab === "admin" ? "active" : ""} onClick={() => openTab("admin")}>Статистика</button>}
      </nav>
      {user ? <div className="account"><span className="avatar">{user.name?.[0] || "A"}</span><span>{user.name}</span><button onClick={logout}>Выйти</button></div>
        : <div className="auth-actions"><button className="register-link" onClick={() => setRegistrationOpen(true)}>Регистрация</button><button className="login" onClick={beginLogin}>Войти</button></div>}
    </header>

    <main>
      {message && <div className="notice">{message}<button onClick={() => setMessage("")}>×</button></div>}
      {tab === "search" && <>
        <section className="hero">
          <div className="eyebrow">БИЛЕТЫ БЕЗ ЛИШНЕЙ ТУРБУЛЕНТНОСТИ</div>
          <h1>Куда летим<br/><em>на этот раз?</em></h1>
          <p>Сравнивайте рейсы, бронируйте места и копите бонусы в одном окне.</p>
          <form className="search" onSubmit={search}>
            <label><span>Откуда</span><input list="origin-cities" minLength={2} maxLength={80} value={origin} onChange={e => setOrigin(e.target.value)} placeholder="Москва" /></label>
            <datalist id="origin-cities">{originSuggestions.map(city => <option value={city} key={city} />)}</datalist>
            <i>→</i>
            <label><span>Куда</span><input list="destination-cities" minLength={2} maxLength={80} value={destination} onChange={e => setDestination(e.target.value)} placeholder="Санкт-Петербург" /></label>
            <datalist id="destination-cities">{destinationSuggestions.map(city => <option value={city} key={city} />)}</datalist>
            <label><span>Пассажиры</span><input type="number" min={1} max={9} value={passengers} onChange={e => setPassengers(Math.max(1, Math.min(9, Number(e.target.value) || 1)))} /></label>
            <button className="primary" disabled={loading}>{loading ? "Ищем…" : "Найти рейсы"}</button>
          </form>
        </section>
        <section className="results">
          <div className="section-head"><div><span>ДОСТУПНЫЕ РЕЙСЫ</span><h2>{routeTitle}</h2></div><b>{flights.length} вариантов</b></div>
          <div className="flight-list">{flights.map(flight => <article className="flight" key={flight.id}>
            <div className="flight-no"><span>✦</span><div><b>{flight.flight_number}</b><small>{flight.aircraft}</small></div></div>
            <div className="leg"><strong>{new Date(flight.departure_at).toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}</strong><b>{flight.origin_code}</b><small>{flight.origin_city}</small></div>
            <div className="line"><small>прямой</small><hr/><span>✈</span></div>
            <div className="leg"><strong>{new Date(flight.arrival_at).toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}</strong><b>{flight.destination_code}</b><small>{flight.destination_city}</small></div>
            <div className="price"><small>{time(flight.departure_at)} · {flight.seats_available} мест</small><strong>{rub.format(flight.price_rub)}</strong><button onClick={() => openBooking(flight)}>Выбрать</button></div>
          </article>)}</div>
        </section>
      </>}

      {tab === "trips" && <section className="page">
        <div className="page-title"><div><span>ЛИЧНЫЙ КАБИНЕТ</span><h1>Мои поездки</h1></div><div className="points"><small>Бонусный баланс</small><b>{dashboard?.loyalty?.points ?? "—"} миль</b><span>{dashboard?.loyalty?.tier ?? "Сервис недоступен"}</span></div></div>
        {dashboard?.warnings.map(w => <div className="warning" key={w}>{w}</div>)}
        <div className="booking-list">{dashboard?.bookings.items.map(booking => <article className="booking" key={booking.id}>
          <div><small>БРОНЬ {booking.booking_code}</small><h3>{booking.origin_code} → {booking.destination_code}</h3><p>{booking.flight_number} · {time(booking.departure_at)} · места {booking.seat_numbers || "—"} · {baggageLabel(booking.baggage_type)}</p><p>{booking.passenger_name}</p></div>
          <div><span className={`status ${booking.checked_in_at ? "confirmed" : booking.status}`}>{booking.checked_in_at ? "Зарегистрирован" : booking.status === "confirmed" ? "Подтверждено" : "Ждёт оплаты"}</span><b>{rub.format(booking.amount_rub)}</b>{booking.status !== "confirmed" && <button className="primary" onClick={() => pay(booking)}>Оплатить</button>}{booking.status === "confirmed" && !booking.checked_in_at && <button className="primary" onClick={() => checkIn(booking)}>Онлайн-регистрация</button>}</div>
        </article>)}</div>
        {!dashboard?.bookings.count && !loading && <div className="empty">Здесь появятся ваши бронирования.</div>}
      </section>}

      {tab === "admin" && <section className="page">
        <div className="page-title"><div><span>ADMIN CONSOLE</span><h1>Сводка за 7 дней</h1></div><button className="primary" onClick={downloadReport}>Скачать CSV</button></div>
        <div className="metrics"><div><small>Событий</small><b>{report?.totals.total_events ?? 0}</b></div><div><small>Пользователей</small><b>{report?.totals.unique_subjects ?? 0}</b></div><div><small>Сервисов</small><b>{new Set(report?.breakdown.map(x => x.service)).size}</b></div></div>
        <div className="table-wrap"><table><thead><tr><th>Сервис</th><th>Действие</th><th>События</th><th>Ошибки</th><th>Доля ошибок</th></tr></thead><tbody>{report?.breakdown.map(row => <tr key={`${row.service}-${row.action}`}><td>{row.service}</td><td>{row.action}</td><td>{row.event_count}</td><td>{row.error_count}</td><td>{row.error_rate || 0}%</td></tr>)}</tbody></table></div>
      </section>}
    </main>

    {selected && <div className="modal-backdrop" onMouseDown={() => setSelected(null)}><form className="modal booking-modal" onSubmit={book} onMouseDown={e => e.stopPropagation()}>
      <button type="button" className="close" onClick={() => setSelected(null)}>×</button><span>БРОНИРОВАНИЕ</span><h2>{selected.origin_code} → {selected.destination_code}</h2><p>{selected.flight_number} · {time(selected.departure_at)} · {passengers} пасс.</p>
      <div className="modal-section"><h3>Пассажир</h3><div className="form-grid">
        <label>ФИО<input required minLength={2} value={passenger.name} onChange={e => setPassenger({ ...passenger, name: e.target.value })} /></label>
        <label>Email<input required type="email" minLength={5} value={passenger.email} onChange={e => setPassenger({ ...passenger, email: e.target.value })} placeholder="ivan@example.com" /></label>
        <label>Телефон<input required minLength={5} value={passenger.phone} onChange={e => setPassenger({ ...passenger, phone: e.target.value })} placeholder="+7 900 000-00-00" /></label>
        <label>Документ<input required minLength={4} value={passenger.document} onChange={e => setPassenger({ ...passenger, document: e.target.value })} placeholder="4012 345678" /></label>
        <label>Дата рождения<input required type="date" value={passenger.birthDate} onChange={e => setPassenger({ ...passenger, birthDate: e.target.value })} /></label>
      </div></div>
      <div className="modal-section"><h3>Места</h3><p>Выберите {passengers} мест(а). Сейчас выбрано: {selectedSeats.join(", ") || "—"}.</p><div className="seat-map">{seatMap.map(seat => <button type="button" key={seat.seat_number} disabled={seat.status !== "available"} className={`seat ${seat.status} ${selectedSeats.includes(seat.seat_number) ? "selected" : ""}`} onClick={() => toggleSeat(seat)}>{seat.seat_number}</button>)}</div></div>
      <div className="modal-section"><h3>Багаж</h3><div className="choice-grid">{baggageOptions.map(option => <button type="button" key={option.value} className={baggageType === option.value ? "choice active" : "choice"} onClick={() => setBaggageType(option.value)}><b>{option.title}</b><span>{option.hint}</span><strong>{option.price ? `+${rub.format(option.price)} / пасс.` : "включено"}</strong></button>)}</div></div>
      <div className="total"><span>Билеты {rub.format(selected.price_rub * passengers)} · багаж {rub.format(selectedBaggageFee)}</span><b>{rub.format(selected.price_rub * passengers + selectedBaggageFee)}</b></div><button className="primary" disabled={loading || selectedSeats.length !== passengers}>Забронировать</button>
    </form></div>}
    {registrationOpen && <div className="modal-backdrop" onMouseDown={() => setRegistrationOpen(false)}><form className="modal" onSubmit={register} onMouseDown={e => e.stopPropagation()}>
      <button type="button" className="close" onClick={() => setRegistrationOpen(false)}>×</button><span>AEROFLOW ID</span><h2>Регистрация</h2><p>Создайте аккаунт для бронирования билетов и бонусной программы.</p>
      <label>Логин<input required minLength={3} maxLength={40} pattern="[a-zA-Z0-9_.-]+" autoComplete="username" value={registration.username} onChange={e => setRegistration({ ...registration, username: e.target.value })} placeholder="ivan.petrov" /></label>
      <label>Имя и фамилия<input required minLength={2} maxLength={120} autoComplete="name" value={registration.full_name} onChange={e => setRegistration({ ...registration, full_name: e.target.value })} placeholder="Иван Петров" /></label>
      <label>Email<input required type="email" minLength={5} maxLength={200} autoComplete="email" value={registration.email} onChange={e => setRegistration({ ...registration, email: e.target.value })} placeholder="ivan@example.com" /></label>
      <label>Пароль<input required type="password" minLength={8} maxLength={128} autoComplete="new-password" value={registration.password} onChange={e => setRegistration({ ...registration, password: e.target.value })} /></label>
      <button className="primary modal-action" disabled={loading}>{loading ? "Создаём…" : "Создать аккаунт"}</button>
    </form></div>}
    <footer><span>© 2026 AeroFlow</span><span>OIDC secured · Kafka observed · Kubernetes ready</span></footer>
  </>;
}

export default App;
