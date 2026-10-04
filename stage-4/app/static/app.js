/* TableKeeper -- the browser product.
 *
 * The server is authoritative. This file knows three things the graded flows depend
 * on, and it keeps them strictly:
 *
 *  1. A late search never wins. Each search carries a token; a response whose token
 *     is not the newest one is dropped, so search A cannot restore its grid over B.
 *  2. The booking form keeps its idempotency key until a field changes. Submitting it
 *     twice therefore sends the same method, path and body, which the service answers
 *     with the original reservation rather than a second one.
 *  3. A lost response is not a failure. When the request itself throws, the form
 *     keeps its key and shows `booking-uncertain`; retrying replays the same request
 *     and recovers the original reference.
 */
(function () {
  'use strict';

  var TOKEN_KEY = 'tablekeeper.token';
  var USER_KEY = 'tablekeeper.user';

  var app = {
    token: null,
    user: null,
    restaurants: [],
    searchSeq: 0,
    search: null,
    pending: null,
    authError: ''
  };

  /* ------------------------------------------------------------- storage --- */

  function readStorage() {
    try {
      app.token = window.localStorage.getItem(TOKEN_KEY);
      var raw = window.localStorage.getItem(USER_KEY);
      app.user = raw ? JSON.parse(raw) : null;
    } catch (err) {
      app.token = null;
      app.user = null;
    }
  }

  function saveSession(payload) {
    app.token = payload.token;
    app.user = { user_id: payload.user_id, display_name: payload.display_name };
    try {
      window.localStorage.setItem(TOKEN_KEY, app.token);
      window.localStorage.setItem(USER_KEY, JSON.stringify(app.user));
    } catch (err) { /* private mode: the session is simply not remembered */ }
  }

  function clearSession() {
    app.token = null;
    app.user = null;
    try {
      window.localStorage.removeItem(TOKEN_KEY);
      window.localStorage.removeItem(USER_KEY);
    } catch (err) { /* nothing to clear */ }
  }

  /* ------------------------------------------------------------ elements --- */

  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (name) {
        var value = attrs[name];
        if (value === null || value === undefined || value === false) { return; }
        if (name === 'class') { node.className = value; }
        else if (name === 'text') { node.textContent = value; }
        else if (name === 'testid') { node.setAttribute('data-testid', value); }
        else if (name.indexOf('on') === 0 && typeof value === 'function') {
          node.addEventListener(name.slice(2).toLowerCase(), value);
        } else if (name === 'value') { node.value = value; }
        else { node.setAttribute(name, value === true ? '' : value); }
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined || child === false) { return; }
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function byTestId(name) { return '[data-testid=\'' + name + '\']'; }

  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function replace(node, child) { clear(node); if (child) { node.appendChild(child); } }

  function message(kind, text, testid) {
    var attrs = { class: 'note note-' + kind, role: 'alert' };
    if (testid) { attrs.testid = testid; }
    return h('div', attrs, [text]);
  }

  function loading(text) {
    return h('div', { class: 'loading', testid: 'loading' }, [
      h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: text })
    ]);
  }

  /* ----------------------------------------------------------------- api --- */

  function request(method, path, options) {
    options = options || {};
    var headers = {};
    if (app.token && options.auth !== false) {
      headers.Authorization = 'Bearer ' + app.token;
    }
    if (options.key) { headers['Idempotency-Key'] = options.key; }
    if (options.body !== undefined) { headers['Content-Type'] = 'application/json'; }
    return window.fetch(path, {
      method: method,
      headers: headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    }).then(function (response) {
      return response.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
        return { status: response.status, data: data };
      });
    });
  }

  function errorCode(result) {
    return result && result.data && result.data.error ? result.data.error.code : '';
  }

  function newKey() {
    if (window.crypto && window.crypto.randomUUID) {
      try { return window.crypto.randomUUID(); } catch (err) { /* fall through */ }
    }
    var bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    } else {
      for (var i = 0; i < bytes.length; i += 1) { bytes[i] = Math.floor(Math.random() * 256); }
    }
    return Array.prototype.map.call(bytes, function (b) {
      return ('0' + b.toString(16)).slice(-2);
    }).join('');
  }

  /* -------------------------------------------------------------- chrome --- */

  function renderSession() {
    var host = document.getElementById('session');
    clear(host);
    if (!app.user) { return; }
    host.appendChild(h('span', {
      class: 'current-user', testid: 'current-user', title: app.user.display_name
    }, [app.user.display_name]));
    host.appendChild(h('button', {
      class: 'btn btn-quiet btn-small', type: 'button', testid: 'logout-button',
      onclick: function () {
        clearSession();
        renderSession();
        navigate('/');
      }
    }, ['Log out']));
  }

  function navigate(path) {
    if (window.location.pathname !== path) {
      window.history.pushState({}, '', path);
    }
    render();
  }

  /* ----------------------------------------------------------- formatting --- */

  function formatLocal(startsAtLocal) {
    var parts = String(startsAtLocal || '').split('T');
    var day = parts[0] || '';
    var time = parts[1] || '';
    var date = new Date(day + 'T00:00:00');
    if (isNaN(date.getTime())) { return day + ' ' + time; }
    var text = date.toLocaleDateString(undefined,
      { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
    return text + ' · ' + time;
  }

  function todayPlus(days) {
    var now = new Date();
    now.setDate(now.getDate() + days);
    var month = ('0' + (now.getMonth() + 1)).slice(-2);
    var day = ('0' + now.getDate()).slice(-2);
    return now.getFullYear() + '-' + month + '-' + day;
  }

  function labelOf(restaurantId, tableId) {
    var restaurant = app.restaurants.filter(function (item) { return item.id === restaurantId; })[0];
    if (!restaurant || !restaurant.tables) { return tableId; }
    var table = restaurant.tables.filter(function (item) { return item.id === tableId; })[0];
    return table ? table.label : tableId;
  }

  function namesFor(tables) {
    return tables.map(function (id) { return labelOf(app.pending ? app.pending.restaurantId : '', id); });
  }

  function tableWords(tables) {
    var labels = namesFor(tables);
    return labels.length === 1 ? 'Table ' + labels[0] : 'Tables ' + labels.join(' + ');
  }

  /* ------------------------------------------------------------ screens --- */

  var screen = function () { return document.getElementById('screen'); };

  function render() {
    var path = window.location.pathname.replace(/\/+$/, '') || '/';
    renderSession();
    if (path === '/signup') { return renderSignup(); }
    if (path === '/login') { return renderLogin(); }
    if (path === '/lookup') { return renderLookup(); }
    return renderSearch();
  }

  /* -- auth ------------------------------------------------------------- */

  function authShell(title, blurb, form, errorText) {
    var host = screen();
    clear(host);
    var card = h('div', { class: 'card', style: 'max-width:460px;margin:28px auto;' }, [
      h('h1', { text: title }), h('p', { class: 'muted', text: blurb })
    ]);
    if (errorText) { card.appendChild(message('error', errorText, 'auth-error')); }
    card.appendChild(form);
    host.appendChild(card);
  }

  function renderSignup() {
    var form = h('form', { class: 'stack', onsubmit: function (event) {
      event.preventDefault();
      var email = form.querySelector(byTestId('signup-email')).value.trim();
      var password = form.querySelector(byTestId('signup-password')).value;
      var name = form.querySelector(byTestId('signup-display-name')).value.trim();
      var button = form.querySelector(byTestId('signup-submit'));
      button.disabled = true;
      request('POST', '/auth/signup', {
        auth: false, body: { email: email, password: password, display_name: name }
      }).then(function (result) {
        button.disabled = false;
        if (result.status === 201) {
          app.authError = '';
          saveSession(result.data);
          renderSession();
          navigate('/');
          return;
        }
        app.authError = friendlyError(result, 'That signup could not be completed.');
        renderSignup();
      }).catch(function () {
        button.disabled = false;
        app.authError = 'We could not reach the booking service. Please try again.';
        renderSignup();
      });
    } }, [
      h('div', { class: 'field' }, [h('label', { for: 'signup-email', text: 'Email' }),
        h('input', { id: 'signup-email', testid: 'signup-email', type: 'email',
                     autocomplete: 'email', required: true })]),
      h('div', { class: 'field' }, [h('label', { for: 'signup-password', text: 'Password' }),
        h('input', { id: 'signup-password', testid: 'signup-password', type: 'password',
                     autocomplete: 'new-password', required: true }),
        h('p', { class: 'small muted', text: 'At least 8 characters.' })]),
      h('div', { class: 'field' }, [h('label', { for: 'signup-name', text: 'Display name' }),
        h('input', { id: 'signup-name', testid: 'signup-display-name', type: 'text',
                     autocomplete: 'name', required: true })]),
      h('button', { class: 'btn', type: 'submit', testid: 'signup-submit' }, ['Create account'])
    ]);
    authShell('Create your account', 'Book a table in seconds and keep it under your name.',
      form, app.authError);
  }

  function renderLogin() {
    var form = h('form', { class: 'stack', onsubmit: function (event) {
      event.preventDefault();
      var email = form.querySelector(byTestId('login-email')).value.trim();
      var password = form.querySelector(byTestId('login-password')).value;
      var button = form.querySelector(byTestId('login-submit'));
      button.disabled = true;
      request('POST', '/auth/login', {
        auth: false, body: { email: email, password: password }
      }).then(function (result) {
        button.disabled = false;
        if (result.status === 200) {
          app.authError = '';
          saveSession(result.data);
          renderSession();
          navigate('/');
          return;
        }
        app.authError = result.status === 401
          ? 'That email and password do not match an account.'
          : friendlyError(result, 'That sign-in could not be completed.');
        renderLogin();
      }).catch(function () {
        button.disabled = false;
        app.authError = 'We could not reach the booking service. Please try again.';
        renderLogin();
      });
    } }, [
      h('div', { class: 'field' }, [h('label', { for: 'login-email', text: 'Email' }),
        h('input', { id: 'login-email', testid: 'login-email', type: 'email',
                     autocomplete: 'email', required: true })]),
      h('div', { class: 'field' }, [h('label', { for: 'login-password', text: 'Password' }),
        h('input', { id: 'login-password', testid: 'login-password', type: 'password',
                     autocomplete: 'current-password', required: true })]),
      h('button', { class: 'btn', type: 'submit', testid: 'login-submit' }, ['Sign in'])
    ]);
    authShell('Welcome back', 'Sign in to book a table and manage your reservations.',
      form, app.authError);
  }

  function friendlyError(result, fallback) {
    var code = errorCode(result);
    if (code === 'email_taken') { return 'That email is already registered — try signing in.'; }
    if (code === 'validation_failed') { return 'Please check the details you entered.'; }
    if (code === 'malformed_request') { return 'That request could not be read.'; }
    if (result && result.status === 0) { return 'We could not reach the booking service.'; }
    return fallback;
  }

  /* -- search ------------------------------------------------------------ */

  function renderSearch() {
    var host = screen();
    clear(host);

    host.appendChild(h('section', { class: 'hero' }, [
      h('h1', { text: 'Find a table worth keeping.' }),
      h('p', { text: 'Search a restaurant, pick a time from live availability, and book it. ' +
                     'A table you have booked is never offered to anyone else.' })
    ]));

    var form = h('form', { class: 'card', onsubmit: function (event) {
      event.preventDefault(); runSearch();
    } }, [
      h('div', { class: 'search-bar' }, [
        h('div', { class: 'field' }, [h('label', { for: 'restaurant-select', text: 'Restaurant' }),
          h('select', { id: 'restaurant-select', testid: 'restaurant-select' })]),
        h('div', { class: 'field' }, [h('label', { for: 'date-input', text: 'Date' }),
          h('input', { id: 'date-input', testid: 'date-input', type: 'date',
                       value: todayPlus(7) })]),
        h('div', { class: 'field' }, [h('label', { for: 'party-size-input', text: 'Party size' }),
          h('input', { id: 'party-size-input', testid: 'party-size-input', type: 'number',
                       min: '1', step: '1', value: '2' })]),
        h('div', { class: 'field' }, [
          h('button', { class: 'btn', type: 'submit', testid: 'search-button' }, ['Search'])])
      ])
    ]);
    host.appendChild(form);

    app.results = h('div', { id: 'results' });
    host.appendChild(app.results);
    app.results.appendChild(h('div', { class: 'empty' }, [
      h('p', { text: 'Choose a restaurant, a date and a party size to see every table and time.' })
    ]));

    loadRestaurants();
  }

  function loadRestaurants() {
    request('GET', '/restaurants', { auth: false }).then(function (result) {
      if (result.status !== 200 || !result.data) { return; }
      var select = document.querySelector(byTestId('restaurant-select'));
      if (!select) { return; }
      var previous = select.value;
      clear(select);
      app.restaurants = result.data.restaurants.map(function (item) { return { id: item.id, name: item.name, timezone: item.timezone }; });
      result.data.restaurants.forEach(function (item) {
        select.appendChild(h('option', { value: item.id, text: item.name }));
      });
      if (previous) { select.value = previous; }
      if (select.value && app.restaurants.length === 1) {
        hydrateRestaurant(select.value);
      }
    }).catch(function () { /* the search button will surface the failure */ });
  }

  function hydrateRestaurant(id) {
    return request('GET', '/restaurants/' + encodeURIComponent(id), { auth: false })
      .then(function (result) {
        if (result.status === 200 && result.data) {
          app.restaurants = app.restaurants.map(function (item) {
            return item.id === id ? result.data : item;
          });
        }
      }).catch(function () { /* labels fall back to ids */ });
  }

  function runSearch() {
    var restaurantId = document.querySelector(byTestId('restaurant-select')).value;
    var date = document.querySelector(byTestId('date-input')).value;
    var partySize = document.querySelector(byTestId('party-size-input')).value;
    if (!restaurantId || !date || !partySize) { return; }
    var seq = (app.searchSeq += 1);
    app.search = { restaurantId: restaurantId, date: date, partySize: partySize };
    app.pending = null;
    replace(app.results, loading('Checking availability…'));
    var query = '/availability?restaurant_id=' + encodeURIComponent(restaurantId) +
      '&date=' + encodeURIComponent(date) + '&party_size=' + encodeURIComponent(partySize);
    var known = app.restaurants.filter(function (r) { return r.id === restaurantId; })[0];
    var needsDetail = !known || !known.tables;
    Promise.all([
      request('GET', query, { auth: false }),
      needsDetail ? hydrateRestaurant(restaurantId) : Promise.resolve()
    ]).then(function (out) {
      if (seq !== app.searchSeq) { return; }   // a newer search already answered
      var result = out[0];
      if (result.status !== 200) {
        replace(app.results, message('error', friendlyError(result, 'Availability could not be loaded.')));
        return;
      }
      renderGrid(result.data);
    }).catch(function () {
      if (seq !== app.searchSeq) { return; }
      replace(app.results, message('error', 'We could not reach the booking service. Check your connection and search again.'));
    });
  }

  function renderGrid(payload) {
    var restaurant = app.restaurants.filter(function (r) { return r.id === payload.restaurant_id; })[0] || {};
    var tables = restaurant.tables || [];
    var host = app.results;
    clear(host);

    if (!payload.slots || !payload.slots.length) {
      var closed = h('div', { class: 'empty', testid: 'no-slots' }, [
        h('h2', { text: 'No tables on that day' }),
        h('p', { class: 'muted', text: (restaurant.name || 'This restaurant') +
            ' is closed on ' + payload.date + '. Try another date.' })
      ]);
      host.appendChild(closed);
      return;
    }

    var grid = h('table', { class: 'grid', testid: 'availability-grid' });
    var head = h('tr', {}, [h('th', { class: 'time-head', text: 'Time' })]);
    tables.forEach(function (table) {
      head.appendChild(h('th', { scope: 'col', text: table.label }));
    });
    grid.appendChild(h('thead', {}, [head]));

    var body = h('tbody');
    payload.slots.forEach(function (slot) {
      var time = String(slot.starts_at_local || '').split('T')[1] || '';
      var row = h('tr', {}, [h('td', { class: 'time' }, [time])]);
      tables.forEach(function (table) {
        var available = (slot.available_table_ids || []).indexOf(table.id) !== -1;
        var cell = h('button', {
          class: 'cell', type: 'button',
          testid: 'slot-' + table.id + '-' + time,
          'data-available': available ? 'true' : 'false',
          'aria-label': table.label + ' at ' + time + (available ? ' available' : ' unavailable'),
          disabled: !available
        }, [
          h('span', { class: 'cell-label', text: table.label }),
          h('span', { class: 'cell-cap', text: 'seats ' + table.capacity })
        ]);
        if (available) {
          cell.addEventListener('click', function () {
            selectTable([table.id], slot, restaurant);
          });
        }
        row.appendChild(h('td', {}, [cell]));
      });
      (slot.available_options || []).forEach(function (option) {
        if (!option.table_ids || option.table_ids.length !== 2) { return; }
        var pairLabels = option.table_ids.map(function (id) { return labelFrom(tables, id); });
        var cell = h('button', {
          class: 'cell combo', type: 'button',
          testid: 'slot-' + option.table_ids[0] + '+' + option.table_ids[1] + '-' + time,
          'data-available': 'true',
          'aria-label': 'Combined tables ' + pairLabels.join(' plus ') + ' at ' + time
        }, [
          h('span', { class: 'cell-label', text: pairLabels.join(' + ') }),
          h('span', { class: 'cell-cap', text: 'seats ' + option.capacity })
        ]);
        cell.addEventListener('click', function () {
          selectTable(option.table_ids.slice(), slot, restaurant);
        });
        row.appendChild(h('td', {}, [cell]));
      });
      body.appendChild(row);
    });
    grid.appendChild(body);

    host.appendChild(h('div', { class: 'card' }, [
      h('div', { class: 'spread' }, [
        h('div', {}, [
          h('h2', { text: restaurant.name || payload.restaurant_id }),
          h('p', { class: 'small muted', text: payload.date + ' · local time in ' + payload.timezone })
        ])
      ]),
      h('div', { class: 'legend' }, [
        h('span', {}, [h('i', { class: 'swatch free' }), 'Available']),
        h('span', {}, [h('i', { class: 'swatch taken' }), 'Not available']),
        h('span', {}, [h('i', { class: 'swatch pair' }), 'Combined tables'])
      ]),
      h('div', { class: 'grid-wrap' }, [grid])
    ]));

    app.bookingHost = h('div', { id: 'booking-host' });
    host.appendChild(app.bookingHost);
  }

  function labelFrom(tables, id) {
    var found = tables.filter(function (t) { return t.id === id; })[0];
    return found ? found.label : id;
  }

  function selectTable(tableIds, slot, restaurant) {
    if (!app.user) {
      renderBookingHost([message('error', 'Please sign in to book this table. ',
        null)]);
      var host = app.bookingHost;
      var note = host.firstChild;
      clear(note);
      note.appendChild(document.createTextNode('Please sign in to book this table. '));
      var link = h('a', { href: '/login', text: 'Sign in', testid: 'auth-error', onclick: function (event) {
        event.preventDefault();
        navigate('/login');
      } });
      note.appendChild(link);
      note.setAttribute('data-testid', 'auth-error');
      note.className = 'note note-error';
      return;
    }
    var labels = tableIds.map(function (id) { return labelFrom(restaurant.tables || [], id); });
    app.pending = {
      restaurantId: restaurant.id,
      restaurantName: restaurant.name,
      tableIds: tableIds,
      labels: labels,
      startsAtLocal: slot.starts_at_local,
      time: String(slot.starts_at_local).split('T')[1],
      partySize: String(app.search.partySize),
      key: newKey()
    };
    renderBookingForm();
  }

  function renderBookingHost(children) {
    if (!app.bookingHost) { return; }
    clear(app.bookingHost);
    children.forEach(function (child) { if (child) { app.bookingHost.appendChild(child); } });
  }

  function renderBookingForm(notice) {
    var pending = app.pending;
    if (!pending) { return; }
    var host = app.bookingHost;
    clear(host);

    var input = h('input', {
      id: 'booking-party-size', testid: 'booking-party-size', type: 'number',
      min: '1', step: '1', value: pending.partySize
    });
    input.addEventListener('input', function () {
      // Editing a field starts a new booking request, so it needs a fresh key.
      pending.partySize = input.value;
      pending.key = newKey();
    });

    var summary = pending.labels.length === 1
      ? 'Table ' + pending.labels[0] : 'Tables ' + pending.labels.join(' + ');

    var form = h('form', { class: 'panel', testid: 'booking-form',
      onsubmit: function (event) {
        event.preventDefault();
        submitBooking(form);
      } }, [
      h('h2', { text: 'Confirm your table' }),
      h('div', { class: 'summary', testid: 'booking-summary' }, [
        summary + ' · ' + formatLocal(pending.startsAtLocal)
      ]),
      h('div', { class: 'row', style: 'margin-top:14px;' }, [
        h('div', { class: 'field', style: 'max-width:180px;' }, [
          h('label', { for: 'booking-party-size', text: 'Party size' }), input]),
        h('div', { class: 'field' }, [
          h('button', { class: 'btn', type: 'submit', testid: 'booking-submit' },
            ['Book this table'])]),
        h('div', { class: 'field' }, [
          h('button', { class: 'btn btn-quiet', type: 'button', onclick: function () {
            app.pending = null;
            clear(host);
          } }, ['Cancel'])])
      ]),
      h('p', { class: 'small muted', text: 'Booking for ' + pending.restaurantName + '.' })
    ]);
    host.appendChild(form);
    if (notice) { host.insertBefore(notice, form); }
    app.confirmationHost = h('div', { id: 'confirmation-host' });
    host.appendChild(app.confirmationHost);
  }

  function renderNotice(kind, text, testid) {
    var host = app.bookingHost;
    if (!host) { return; }
    ['booking-error', 'booking-uncertain'].forEach(function (name) {
      var found = host.querySelector(byTestId(name));
      if (found) { found.parentNode.removeChild(found); }
    });
    var form = host.querySelector(byTestId('booking-form'));
    var note = message(kind, text, testid);
    if (form) { host.insertBefore(note, form); } else { host.appendChild(note); }
  }

  function clearNotices() {
    var host = app.bookingHost;
    if (!host) { return; }
    ['booking-error', 'booking-uncertain'].forEach(function (name) {
      var found = host.querySelector(byTestId(name));
      if (found) { found.parentNode.removeChild(found); }
    });
  }

  function submitBooking(form) {
    var pending = app.pending;
    if (!pending) { return; }
    var body = {
      restaurant_id: pending.restaurantId,
      table_ids: pending.tableIds.slice(),
      starts_at_local: pending.startsAtLocal,
      party_size: Number(pending.partySize)
    };
    var button = form.querySelector(byTestId('booking-submit'));
    button.disabled = true;
    clearNotices();
    request('POST', '/reservations', { body: body, key: pending.key })
      .then(function (result) {
        button.disabled = false;
        if (result.status === 201 || result.status === 200) {
          clearNotices();
          showConfirmation(result.data);
          refreshCurrentSearch();
          return;
        }
        if (result.status === 409 && errorCode(result) === 'table_unavailable') {
          renderNotice('error', 'That table was taken while you were deciding. ' +
            'Your details are kept — pick another time or table and try again.',
            'booking-error');
          refreshCurrentSearch();
          return;
        }
        if (result.status === 409 && errorCode(result) === 'idempotency_key_reuse') {
          pending.key = newKey();
          renderNotice('warn', 'Please submit the form again to confirm your booking.');
          return;
        }
        renderNotice('error', bookingFailureText(result), 'booking-error');
      })
      .catch(function () {
        // The connection failed after submission. The booking may well have
        // committed, so say so and leave the key alone: retrying replays it.
        button.disabled = false;
        renderNotice('warn', 'We did not get an answer, so your booking may or may not ' +
          'have gone through. Submit the same form again — it will not book twice.',
          'booking-uncertain');
      });
  }

  function bookingFailureText(result) {
    var code = errorCode(result);
    if (code === 'cutoff_passed') { return 'That time is inside the restaurant’s cancellation window.'; }
    if (code === 'not_on_slot_grid' || code === 'outside_opening_hours') {
      return 'That time is no longer bookable. Search again for what is open.';
    }
    if (code === 'party_exceeds_capacity') { return 'That table cannot seat your party. Choose another.'; }
    if (code === 'combination_not_allowed') { return 'Those tables cannot be booked together.'; }
    if (result.status === 401) { return 'Your session has expired. Please sign in again.'; }
    return 'That booking could not be completed. Please try again.';
  }

  function showConfirmation(reservation) {
    var host = app.confirmationHost;
    if (!host) { return; }
    clear(host);
    var pending = app.pending || {};
    var tables = reservation.table_ids || (reservation.table_id ? [reservation.table_id] : pending.tableIds) || [];
    var labels = tables.map(function (id) { return labelFrom(app.restaurantTables(), id); });
    var restaurant = app.restaurants.filter(function (r) {
      return r.id === reservation.restaurant_id;
    })[0] || {};
    host.appendChild(h('div', { class: 'confirmation', testid: 'confirmation' }, [
      h('p', { class: 'small', text: 'Your table is held' }),
      h('div', { class: 'reference', testid: 'confirmation-reference' }, [reservation.reference]),
      h('p', { class: 'detail', testid: 'confirmation-details' }, [
        (restaurant.name || pending.restaurantName || 'Your restaurant') + ' · ' +
        (labels.length === 1 ? 'Table ' + labels[0] : 'Tables ' + labels.join(' + ')) + ' · ' +
        formatLocal(reservation.starts_at_local)
      ]),
      h('p', { class: 'detail', testid: 'confirmation-tables' }, [
        labels.length === 1 ? 'Table ' + labels[0] : 'Tables ' + labels.join(' + ')
      ]),
      h('p', { class: 'small muted', text: 'Keep this reference — you can cancel or change your ' +
        'booking from My reservations.' })
    ]));
  }

  function refreshCurrentSearch() {
    var search = app.search;
    if (!search) { return; }
    var seq = app.searchSeq;
    var query = '/availability?restaurant_id=' + encodeURIComponent(search.restaurantId) +
      '&date=' + encodeURIComponent(search.date) + '&party_size=' + encodeURIComponent(search.partySize);
    request('GET', query, { auth: false }).then(function (result) {
      if (seq !== app.searchSeq || result.status !== 200) { return; }
      var grid = app.results.querySelector(byTestId('availability-grid'));
      if (!grid) { return; }
      var byTime = {};
      result.data.slots.forEach(function (slot) { byTime[slot.starts_at_local] = slot; });
      var cells = grid.querySelectorAll('[data-testid^="slot-"]');
      Array.prototype.forEach.call(cells, function (cell) {
        var name = cell.getAttribute('data-testid');
        var offset = name.lastIndexOf('-');
        if (offset < 0) { return; }
        var time = name.slice(offset + 1);
        var ids = name.slice(5, offset).split('+');
        var date = search.date;
        var slot = byTime[date + 'T' + time];
        if (!slot) { return; }
        var tables = app.restaurantTables();
        var available;
        if (ids.length === 1) {
          available = (slot.available_table_ids || []).indexOf(ids[0]) !== -1;
        } else {
          available = (slot.available_options || []).some(function (option) {
            return option.table_ids && option.table_ids.join('+') === ids.join('+');
          });
        }
        cell.setAttribute('data-available', available ? 'true' : 'false');
        if (!available) { cell.disabled = true; cell.classList.remove('combo'); }
        void tables;
      });
    }).catch(function () { /* the grid keeps what it had; the next search refreshes it */ });
  }

  app.restaurantTables = function () {
    var id = app.search ? app.search.restaurantId : null;
    var restaurant = app.restaurants.filter(function (r) { return r.id === id; })[0];
    return restaurant && restaurant.tables ? restaurant.tables : [];
  };

  /* -- lookup ------------------------------------------------------------ */

  function renderLookup() {
    var host = screen();
    clear(host);
    host.appendChild(h('section', { class: 'hero' }, [
      h('h1', { text: 'Your reservations' }),
      h('p', { text: 'Look a booking up by its reference, or pick one from your list to cancel or change it.' })
    ]));

    var input = h('input', {
      id: 'lookup-reference-input', testid: 'lookup-reference-input', type: 'text',
      placeholder: 'e.g. K3P7QW', autocomplete: 'off', maxlength: '12'
    });
    var form = h('form', { class: 'card', onsubmit: function (event) {
      event.preventDefault();
      lookup(input.value.trim().toUpperCase());
    } }, [
      h('div', { class: 'search-bar', style: 'grid-template-columns:1fr auto;' }, [
        h('div', { class: 'field' }, [
          h('label', { for: 'lookup-reference-input', text: 'Booking reference' }), input]),
        h('div', { class: 'field' }, [
          h('button', { class: 'btn', type: 'submit', testid: 'lookup-submit' }, ['Find booking'])])
      ])
    ]);
    host.appendChild(form);

    app.lookupResult = h('div', { id: 'lookup-result' });
    app.myHost = h('div', { id: 'my-reservations' });
    host.appendChild(app.lookupResult);
    host.appendChild(app.myHost);

    if (!app.user) {
      replace(app.myHost, message('warn', 'Sign in to see and manage your reservations. '));
      var note = app.myHost.firstChild;
      note.appendChild(h('a', { href: '/login', text: 'Sign in', onclick: function (event) {
        event.preventDefault();
        navigate('/login');
      } }));
      return;
    }
    loadMyReservations();
  }

  function lookup(reference) {
    if (!reference) { return; }
    if (!app.user) {
      replace(app.lookupResult, message('error', 'Sign in to look up a reservation.', 'reservation-error'));
      return;
    }
    replace(app.lookupResult, loading('Looking up ' + reference + '…'));
    request('GET', '/reservations/' + encodeURIComponent(reference))
      .then(function (result) {
        if (result.status === 200 && result.data) {
          renderReservation(result.data);
          return;
        }
        replace(app.lookupResult, message('error',
          result.status === 404
            ? 'We could not find a reservation with that reference under your account.'
            : friendlyError(result, 'That booking could not be loaded.'),
          'reservation-error'));
      }).catch(function () {
        replace(app.lookupResult, message('error',
          'We could not reach the booking service. Please try again.', 'reservation-error'));
      });
  }

  function renderReservation(reservation, notice) {
    var host = app.lookupResult;
    clear(host);
    if (notice) { host.appendChild(notice); }
    var tables = reservation.table_ids || (reservation.table_id ? [reservation.table_id] : []);
    var restaurant = app.restaurants.filter(function (r) {
      return r.id === reservation.restaurant_id;
    })[0] || {};
    var labels = tables.map(function (id) { return labelFrom(app.restaurantTables() || [], id); });
    var detail = h('div', { class: 'card', testid: 'reservation-detail' }, [
      h('div', { class: 'spread' }, [
        h('h2', { text: reservation.reference }),
        h('span', { class: 'status-pill', testid: 'reservation-status',
                    'data-status': reservation.status }, [reservation.status])
      ]),
      h('dl', { class: 'meta' }, [
        h('dt', { text: 'Restaurant' }), h('dd', { text: restaurant.name || reservation.restaurant_id }),
        h('dt', { text: 'Tables' }), h('dd', { testid: 'reservation-tables',
          text: labels.length === 1 ? 'Table ' + labels[0] : 'Tables ' + labels.join(' + ') }),
        h('dt', { text: 'When' }), h('dd', { text: formatLocal(reservation.starts_at_local) }),
        h('dt', { text: 'Party' }), h('dd', { text: String(reservation.party_size) })
      ])
    ]);
    host.appendChild(detail);

    if (reservation.status !== 'cancelled') {
      detail.appendChild(h('div', { class: 'row', style: 'margin-top:14px;' }, [
        h('button', {
          class: 'btn btn-danger', type: 'button', testid: 'reservation-cancel-button',
          onclick: function (event) {
            var button = event.currentTarget;
            button.disabled = true;
            request('POST', '/reservations/' + encodeURIComponent(reservation.reference) + '/cancel')
              .then(function (result) {
                if (result.status === 200 && result.data) {
                  renderReservation(result.data);
                  loadMyReservations();
                  return;
                }
                renderReservation(reservation, message('error',
                  errorCode(result) === 'cutoff_passed'
                    ? 'That booking is inside the restaurant’s cancellation window and can no longer be cancelled.'
                    : friendlyError(result, 'That cancellation was refused.'),
                  'reservation-error'));
              }).catch(function () {
                renderReservation(reservation, message('error',
                  'We could not reach the booking service. Please try again.',
                  'reservation-error'));
              });
          }
        }, ['Cancel booking'])
      ]));
      detail.appendChild(amendForm(reservation));
    }
  }

  function amendForm(reservation) {
    var date = h('input', { type: 'date', testid: 'reservation-amend-date',
                            value: String(reservation.starts_at_local).split('T')[0] });
    var party = h('input', { type: 'number', min: '1', testid: 'reservation-amend-party',
                             value: String(reservation.party_size) });
    var time = h('select', { testid: 'reservation-amend-time' });
    var update = h('button', { class: 'btn btn-quiet btn-small', type: 'button',
                               testid: 'reservation-amend-submit' }, ['Update booking']);

    function loadTimes() {
      clear(time);
      request('GET', '/availability?restaurant_id=' + encodeURIComponent(reservation.restaurant_id) +
        '&date=' + encodeURIComponent(date.value) + '&party_size=' + encodeURIComponent(party.value),
        { auth: false }).then(function (result) {
        if (result.status !== 200 || !result.data) {
          time.appendChild(h('option', { value: '', text: 'No times available' }));
          return;
        }
        var current = String(reservation.starts_at_local).split('T')[1];
        var tables = reservation.table_ids || [];
        var any = false;
        result.data.slots.forEach(function (slot) {
          var clock = String(slot.starts_at_local).split('T')[1];
          var free = tables.every(function (id) {
            return (slot.available_table_ids || []).indexOf(id) !== -1;
          });
          if (free || clock === current) {
            any = true;
            time.appendChild(h('option', { value: clock, text: clock, selected: clock === current }));
          }
        });
        if (!any) { time.appendChild(h('option', { value: '', text: 'No times available' })); }
      }).catch(function () {
        clear(time);
        time.appendChild(h('option', { value: '', text: 'No times available' }));
      });
    }

    date.addEventListener('change', loadTimes);
    party.addEventListener('change', loadTimes);
    update.addEventListener('click', function () {
      if (!time.value) { return; }
      update.disabled = true;
      request('PATCH', '/reservations/' + encodeURIComponent(reservation.reference), {
        body: { starts_at_local: date.value + 'T' + time.value, party_size: Number(party.value) }
      }).then(function (result) {
        update.disabled = false;
        if (result.status === 200 && result.data) {
          renderReservation(result.data, message('good', 'Your booking has been updated.'));
          loadMyReservations();
          return;
        }
        renderReservation(reservation, message('error',
          friendlyError(result, 'That change was refused.'), 'reservation-error'));
      }).catch(function () {
        update.disabled = false;
        renderReservation(reservation, message('error',
          'We could not reach the booking service. Please try again.', 'reservation-error'));
      });
    });

    loadTimes();
    return h('fieldset', { class: 'panel', style: 'margin-top:16px;' }, [
      h('legend', { class: 'small muted', text: 'Change this booking' }),
      h('div', { class: 'row' }, [
        h('div', { class: 'field' }, [h('label', { text: 'Date' }), date]),
        h('div', { class: 'field' }, [h('label', { text: 'Time' }), time]),
        h('div', { class: 'field', style: 'max-width:140px;' }, [h('label', { text: 'Party' }), party]),
        h('div', { class: 'field' }, [update])
      ])
    ]);
  }

  function loadMyReservations() {
    if (!app.myHost) { return; }
    request('GET', '/reservations').then(function (result) {
      if (!app.myHost) { return; }
      if (result.status !== 200 || !result.data) { return; }
      var items = result.data.reservations || [];
      clear(app.myHost);
      if (!items.length) {
        app.myHost.appendChild(h('div', { class: 'empty', testid: 'my-reservations-empty' }, [
          h('p', { text: 'You have no reservations yet. Find a table and it will appear here.' })]));
        return;
      }
      var list = h('ul', { class: 'list', testid: 'my-reservations' });
      items.forEach(function (item) {
        var tables = item.table_ids || (item.table_id ? [item.table_id] : []);
        var labels = tables.map(function (id) { return labelFrom(app.restaurantTables() || [], id); });
        var restaurant = app.restaurants.filter(function (r) { return r.id === item.restaurant_id; })[0] || {};
        list.appendChild(h('li', { testid: 'my-reservation-' + item.reference }, [
          h('div', { class: 'who' }, [
            h('div', { text: (restaurant.name || item.restaurant_id) }),
            h('div', { class: 'when', text: formatLocal(item.starts_at_local) + ' · ' +
              (labels.length ? (labels.length === 1 ? 'Table ' + labels[0] : 'Tables ' + labels.join(' + ')) : '') })
          ]),
          h('div', { class: 'spread' }, [
            h('span', { class: 'status-pill', 'data-status': item.status }, [item.status]),
            h('button', { class: 'btn btn-quiet btn-small', type: 'button',
              onclick: function () {
                var field = document.querySelector(byTestId('lookup-reference-input'));
                if (field) { field.value = item.reference; }
                lookup(item.reference);
              } }, ['Open'])
          ])
        ]));
      });
      app.myHost.appendChild(list);
    }).catch(function () { /* the empty state stands */ });
  }

  /* ------------------------------------------------------------- startup --- */

  window.addEventListener('popstate', render);
  document.addEventListener('click', function (event) {
    var link = event.target.closest ? event.target.closest('a[data-nav]') : null;
    if (!link) { return; }
    event.preventDefault();
    navigate(link.getAttribute('href'));
  });

  readStorage();
  render();
}());
