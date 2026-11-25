/* fha-map.js: the family map (map.html).
 *
 * A cousin opens map.html and sees an outline of the world, zoomed to where
 * the family's records put people, with a pin at each place. Clicking a pin
 * (or pressing Enter on it) shows the place's name, how many events in the
 * family's records happened there and who they place there, with links to their pages and to the place page. It
 * works from a USB stick with no internet: the outline is vendor/fha-world.js
 * (Natural Earth 1:110m, as window.FHA_WORLD), drawn with the vendored D3 and
 * topojson-client, and the pins are the JSON fha site writes into the page
 * (<script type="application/json" id="fha-map-data">). Nothing is fetched or
 * stored. Without script the page's own list of places stands in for the map.
 *
 * Projection: Mercator for a family in one region (the shape OpenStreetMap
 * shows), Natural Earth when the pins span more than 60 degrees of longitude
 * or 40 of latitude. The view is fitted to the pins, widened to at least
 * 5 degrees each way so one pin, or a few in one town, does not zoom in
 * without end. Pins either side of the date line are drawn the long way
 * round the world (a known limit). With a dozen pins or fewer each carries
 * its place name; past that the names would cover each other, so a name
 * shows on the pin chosen or pointed at.
 *
 * Journeys: faint lines show how each person who has died moved, from the
 * place of one record to the next in date order (fha site works the stops
 * out; this file only draws them). Picking a person in the "Show one
 * person's moves" list, or the Show moves button beside their name in a
 * pin's panel, draws their line in the accent colour, numbers the stops,
 * puts an arrow halfway along each move, and lists the stops as text in
 * the panel, the form a screen reader gets. Each move is drawn
 * as the shortest route over the globe, so a crossing from Europe curves.
 * Past FAINT_MAX journeys the faint lines are left out (too many to read,
 * and slow); the picked one still draws.
 *
 * Years: when there are lines, a slider under the map runs from the first
 * year on any line to the last. Dragging it back shows where the family
 * had got to by then: each move once the year it was made by is reached,
 * each pin from the first year a line reaches it. "Play the years" steps a
 * decade a second (any click stops it) and "Show all" puts everything
 * back, pins no line reaches included. The slider is a plain range input
 * with its year written beside it, so the keyboard and a screen reader
 * can use it.
 *
 * Text from the data goes in as text nodes and attributes, never as HTML.
 * Also loadable from node (module.exports), so the build's tests can check
 * the projection maths against the real D3. */
(function () {
  'use strict';

  var VIEW_W = 960;
  var VIEW_H = 540;
  var PAD = 40;            // px kept clear round the pins
  var MIN_HALF_SPAN = 5;   // degrees each way from the pins' centre
  var WIDE_LON = 60;       // degrees of longitude past which the world projection is used
  var WIDE_LAT = 40;       // the same for latitude
  var MAX_PIN = 12;        // px, the biggest pin
  var LABEL_ALL = 12;      // pins up to which every pin shows its name
  var MERCATOR_LAT = 85;   // Mercator cannot show the poles
  var FAINT_MAX = 200;     // journeys past which no faint lines are drawn
  var FAINT_WIDTH = 1.5;   // px on screen, a faint journey line
  var FOCUS_WIDTH = 3;     // px on screen, the followed journey's line

  /* The pins' extent in degrees, or null when there are none. */
  function bounds(places) {
    if (!places || !places.length) return null;
    var b = {minLat: 90, maxLat: -90, minLon: 180, maxLon: -180};
    places.forEach(function (p) {
      b.minLat = Math.min(b.minLat, p.lat);
      b.maxLat = Math.max(b.maxLat, p.lat);
      b.minLon = Math.min(b.minLon, p.lon);
      b.maxLon = Math.max(b.maxLon, p.lon);
    });
    return b;
  }

  /* 'mercator' for pins in one region, else 'naturalEarth'. */
  function projectionKind(places) {
    var b = bounds(places);
    if (!b) return 'naturalEarth';
    return (b.maxLon - b.minLon > WIDE_LON || b.maxLat - b.minLat > WIDE_LAT)
      ? 'naturalEarth' : 'mercator';
  }

  /* What the view is fitted to: the corners of the pins' extent, widened to
   * at least MIN_HALF_SPAN degrees each way round its centre. The whole
   * world when there are no pins. */
  function fitTarget(places, kind) {
    var b = bounds(places);
    if (!b) return {type: 'Sphere'};
    var cLat = (b.minLat + b.maxLat) / 2;
    var cLon = (b.minLon + b.maxLon) / 2;
    var hLat = Math.max((b.maxLat - b.minLat) / 2, MIN_HALF_SPAN);
    var hLon = Math.max((b.maxLon - b.minLon) / 2, MIN_HALF_SPAN);
    var lo = Math.max(cLat - hLat, -90);
    var hi = Math.min(cLat + hLat, 90);
    if (kind === 'mercator') {
      lo = Math.max(lo, -MERCATOR_LAT);
      hi = Math.min(hi, MERCATOR_LAT);
    }
    var w = cLon - hLon;
    var e = cLon + hLon;
    return {type: 'MultiPoint', coordinates: [[w, lo], [e, lo], [w, hi], [e, hi]]};
  }

  /* A D3 projection fitted to the pins inside a w x h frame. */
  function makeProjection(d3, places, w, h) {
    var kind = projectionKind(places);
    var projection = kind === 'mercator' ? d3.geoMercator() : d3.geoNaturalEarth1();
    return projection.fitExtent([[PAD, PAD], [w - PAD, h - PAD]], fitTarget(places, kind));
  }

  /* A pin's radius in px: bigger for a place with more records, at most MAX_PIN. */
  function pinRadius(events) {
    return Math.min(MAX_PIN, 6 + Math.sqrt(Math.max(1, events || 0)));
  }

  function plural(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  /* A stop's years: "1840", or "1870 to 1900" for several records in a row there. */
  function stopYears(stop) {
    return stop.year_from === stop.year_to
      ? String(stop.year_from) : stop.year_from + ' to ' + stop.year_to;
  }

  /* One stop as a line of text: "Fairview, 1870 to 1900, census, residence". */
  function stopText(stop, place) {
    return [place ? place.name : stop.place, stopYears(stop)].concat(stop.what).join(', ');
  }

  /* The moves of one journey, each from one stop's pin to the next as
   * [lon, lat] pairs, with the year of the later stop (the move happened
   * by then). A stop whose pin is missing is skipped. */
  function journeySegments(journey, byId) {
    var points = [];
    journey.stops.forEach(function (stop) {
      var p = byId[stop.place];
      if (p) points.push({at: [p.lon, p.lat], year: stop.year_from});
    });
    var segments = [];
    for (var i = 1; i < points.length; i++) {
      segments.push({from: points[i - 1].at, to: points[i].at, year: points[i].year});
    }
    return segments;
  }

  /* The numbers to write beside each pin of a followed journey: a place
   * the person came back to carries both ("1, 3"). [{place, label}] in
   * first-visit order. */
  function stopNumbers(journey) {
    var order = [];
    var numbers = {};
    journey.stops.forEach(function (stop, i) {
      if (!numbers[stop.place]) {
        numbers[stop.place] = [];
        order.push(stop.place);
      }
      numbers[stop.place].push(i + 1);
    });
    return order.map(function (id) { return {place: id, label: numbers[id].join(', ')}; });
  }

  /* The year slider. A cousin drags it back to see where the family had
   * got to by then: a move shows once its year (the later stop's) is at or
   * before the slider, a pin from the first year any line reaches it. The
   * years come only from the lines, which fha site has already cleared of
   * anyone the build hides. `upTo` null means every year: the slider at
   * its last year, or "Show all", which also brings back the pins no line
   * reaches. */

  /* {place id: the first year a line's stop there starts}. */
  function pinYears(journeys) {
    var first = {};
    (journeys || []).forEach(function (j) {
      j.stops.forEach(function (stop) {
        if (!(stop.place in first) || stop.year_from < first[stop.place]) {
          first[stop.place] = stop.year_from;
        }
      });
    });
    return first;
  }

  /* Whether something from `year` shows with the slider at `upTo`. Every
   * year shows everything; a year that is not known waits for that. */
  function shownBy(year, upTo) {
    if (upTo === null || upTo === undefined) return true;
    return year !== undefined && year !== null && year <= upTo;
  }

  /* The words beside the slider, also its aria-valuetext. */
  function yearText(upTo, years) {
    if (upTo === null || upTo === undefined || upTo >= years[1]) {
      return 'Every year, up to ' + years[1];
    }
    return 'Up to ' + upTo;
  }

  var PLAY_STEP = 10;      // years per step of "Play the years"
  var PLAY_MS = 1000;      // ms per step

  /* True when the viewer has asked their system for less motion. Playing
   * the years steps the map on its own, so then it is left out and the
   * slider and "Show all" do the work. */
  function motionReduced(win) {
    try {
      return !!(win && win.matchMedia &&
                win.matchMedia('(prefers-reduced-motion: reduce)').matches);
    } catch (e) { return false; }
  }

  /* Where "Play the years" begins: from the slider's year, or from the
   * first year when the slider is at the end. */
  function playStart(current, years) {
    return current >= years[1] ? years[0] : current;
  }

  /* The next year Play moves to: a decade on, never past the last year;
   * null once it is there. */
  function nextPlayYear(current, years) {
    if (current >= years[1]) return null;
    return Math.min(years[1], current + PLAY_STEP);
  }

  /* What a journey's line leaves out, as a sentence, or ''. */
  function leftOutText(journey) {
    var parts = [];
    if (journey.undated) parts.push(plural(journey.undated, 'event') + ' with no date');
    if (journey.unplaced) {
      parts.push((journey.undated ? journey.unplaced : plural(journey.unplaced, 'event'))
        + ' at a place with no map position yet');
    }
    return parts.length ? 'Not on this line: ' + parts.join(' and ') + '.' : '';
  }

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text != null) node.appendChild(document.createTextNode(text));
    if (cls) node.className = cls;
    return node;
  }

  function link(href, text) {
    var a = el('a', text);
    a.setAttribute('href', href);
    return a;
  }

  /* Fill the panel beside the map with one place: its name, where it is,
   * how many records put family there, the people, and its page. */
  function clear(panel) {
    while (panel.firstChild) panel.removeChild(panel.firstChild);
  }

  /* `follow(href)` is given when some people have journeys: it returns a
   * function that draws that person's line, or null when they have none. */
  function showPlace(panel, place, follow) {
    clear(panel);
    panel.appendChild(el('h2', place.name));
    if (place.label && place.label !== place.name) panel.appendChild(el('p', place.label, 'note'));
    panel.appendChild(el('p', plural(place.events, 'event') + " in the family's records."));
    if (place.people.length) {
      var list = el('ul', null, 'map-people');
      place.people.forEach(function (person) {
        var li = el('li');
        li.appendChild(link(person.href, person.name));
        var go = follow && follow(person.href);
        if (go) {
          var button = el('button', 'Show moves', 'map-follow');
          button.setAttribute('type', 'button');
          button.setAttribute('aria-label', 'Show ' + person.name + "'s moves on the map");
          button.addEventListener('click', go);
          li.appendChild(document.createTextNode(' '));
          li.appendChild(button);
        }
        list.appendChild(li);
      });
      panel.appendChild(list);
      if (place.more_people) {
        var more = place.more_people;
        panel.appendChild(el('p', 'and ' + more + (more === 1 ? ' more person' : ' more people')
          + ' on the place page.', 'note'));
      }
    }
    var p = el('p');
    p.appendChild(link(place.href, 'Open the page for ' + place.name));
    panel.appendChild(p);
  }

  /* Fill the panel with one person's journey: who, their years, each stop
   * in order (the same numbers the map shows), what the line leaves out. */
  function showJourney(panel, journey, byId) {
    clear(panel);
    panel.appendChild(el('h2', journey.name));
    if (journey.life) panel.appendChild(el('p', journey.life, 'note'));
    var list = el('ol', null, 'map-stops');
    journey.stops.forEach(function (stop) {
      list.appendChild(el('li', stopText(stop, byId[stop.place])));
    });
    panel.appendChild(list);
    var left = leftOutText(journey);
    if (left) panel.appendChild(el('p', left, 'note'));
    var p = el('p');
    p.appendChild(link(journey.href, 'Open the page for ' + journey.name));
    panel.appendChild(p);
  }

  /* The controls under the map: the slider, its year in words, "Play the
   * years" and "Show all". Returns {start(onYear)}, which shows the controls
   * and calls onYear(year or null) whenever the year changes; null when the
   * page has no slider (no lines) or the data has no years. */
  function yearSlider(doc, years) {
    var box = doc.getElementById('fha-map-years');
    var input = doc.getElementById('fha-map-year');
    var text = doc.getElementById('fha-map-year-text');
    var play = doc.getElementById('fha-map-play');
    var all = doc.getElementById('fha-map-all');
    if (!box || !input || !years || years.length !== 2) return null;
    var onYear = function () {};
    var timer = null;

    function set(year) {
      input.value = String(year);
      var upTo = year >= years[1] ? null : year;
      var words = yearText(upTo, years);
      input.setAttribute('aria-valuetext', words);
      if (text) text.textContent = words;
      onYear(upTo);
    }

    function stop() {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
      doc.removeEventListener('click', stopOnClick, true);
      if (play) {
        play.textContent = 'Play the years';
        play.setAttribute('aria-pressed', 'false');
      }
    }

    // Any click on the page stops the playing; the Play button's own click
    // is left to the button, which stops it too.
    function stopOnClick(event) {
      if (event.target !== play) stop();
    }

    function start() {
      set(playStart(Number(input.value), years));
      timer = setInterval(function () {
        var next = nextPlayYear(Number(input.value), years);
        if (next === null) stop();
        else set(next);
      }, PLAY_MS);
      doc.addEventListener('click', stopOnClick, true);
      if (play) {
        play.textContent = 'Stop';
        play.setAttribute('aria-pressed', 'true');
      }
    }

    return {start: function (callback) {
      onYear = callback;
      input.addEventListener('input', function () {
        stop();
        set(Number(input.value));
      });
      if (play && motionReduced(doc.defaultView)) {
        play.hidden = true;
      } else if (play) {
        play.setAttribute('aria-pressed', 'false');
        play.addEventListener('click', function () {
          if (timer === null) start();
          else stop();
        });
      }
      if (all) {
        all.addEventListener('click', function () {
          stop();
          set(years[1]);
        });
      }
      box.hidden = false;
    }};
  }

  function draw(d3, topojson, world, data, box, panel, reset, picker, years) {
    var places = data.places.slice().sort(function (a, b) { return b.events - a.events; });
    var journeys = data.journeys || [];
    var byId = {};
    data.places.forEach(function (p) { byId[p.id] = p; });
    var projection = makeProjection(d3, data.places, VIEW_W, VIEW_H);
    var path = d3.geoPath(projection);
    var svg = d3.select(box).append('svg')
      .attr('class', 'map-svg')
      .attr('viewBox', '0 0 ' + VIEW_W + ' ' + VIEW_H)
      .attr('preserveAspectRatio', 'xMidYMid meet');
    // The arrow halfway along each move of a followed journey. Sized by the
    // line's width, which the zoom keeps constant on screen.
    svg.append('defs').append('marker')
      .attr('id', 'fha-map-arrow')
      .attr('viewBox', '0 0 10 10')
      .attr('refX', 5).attr('refY', 5)
      .attr('markerWidth', 4).attr('markerHeight', 4)
      .attr('orient', 'auto')
      .append('path').attr('class', 'map-arrow').attr('d', 'M0,0L10,5L0,10z');
    var layer = svg.append('g');
    var k = 1;   // the zoom's scale, so lines and labels drawn later match it
    layer.append('path').attr('class', 'map-land')
      .attr('d', path(topojson.feature(world, world.objects.land)));
    layer.append('path').attr('class', 'map-borders')
      .attr('d', path(topojson.mesh(world, world.objects.countries, function (a, b) { return a !== b; })));

    // Everyone's journeys, faint, under the pins: one path per move, so
    // the year slider can hide the moves that come later.
    var faint = layer.append('g').attr('class', 'map-journeys');
    if (journeys.length <= FAINT_MAX) {
      journeys.forEach(function (j) {
        journeySegments(j, byId).forEach(function (s) {
          faint.append('path').datum(s).attr('class', 'map-journey')
            .attr('stroke-width', FAINT_WIDTH)
            .attr('d', path({type: 'LineString', coordinates: [s.from, s.to]}));
        });
      });
    }
    var upTo = null;   // the year slider's year; null is every year
    var firstYear = pinYears(journeys);
    var focus = layer.append('g').attr('class', 'map-journey-focus');

    // Biggest first, so a small pin is never hidden under a big one.
    var pins = layer.append('g').selectAll('g').data(places).enter().append('g')
      .attr('class', 'map-pin')
      .attr('tabindex', 0)
      .attr('role', 'button')
      .attr('aria-label', function (d) { return d.name + ': ' + plural(d.events, 'event'); })
      .attr('transform', function (d) {
        var xy = projection([d.lon, d.lat]);
        return 'translate(' + xy[0] + ',' + xy[1] + ')';
      });
    pins.append('circle').attr('r', function (d) { return pinRadius(d.events); });
    pins.append('title').text(function (d) { return d.name + ': ' + plural(d.events, 'event'); });
    pins.append('text')
      .attr('class', 'map-label')
      .attr('x', function (d) { return pinRadius(d.events) + 3; })
      .attr('dy', '0.35em')
      .text(function (d) { return d.name; });
    svg.classed('map-few', places.length <= LABEL_ALL);
    // The followed journey's stop numbers, above the pins.
    var numbers = layer.append('g').attr('class', 'map-stop-numbers');

    function at(lonLat, scale) {
      var xy = projection(lonLat);
      return 'translate(' + xy[0] + ',' + xy[1] + ') scale(' + scale + ')';
    }

    var followed = -1;
    function follow(index) {
      var was = followed;
      followed = index;
      svg.classed('map-following', index >= 0);
      if (picker) picker.value = index >= 0 ? String(index) : '';
      drawFollowed();
      if (index < 0) {
        if (was >= 0) {   // the journey's text goes with its line
          clear(panel);
          panel.appendChild(el('p', 'Choose a pin to see the place and the people the records put there.', 'note'));
        }
        return;
      }
      pins.classed('is-active', false);
      showJourney(panel, journeys[index], byId);
    }

    /* The followed journey's line and numbers, as far as the year slider
     * goes (the panel still lists every stop). */
    function drawFollowed() {
      focus.selectAll('*').remove();
      numbers.selectAll('*').remove();
      if (followed < 0) return;
      var all = journeys[followed];
      var j = {stops: all.stops.filter(function (s) { return shownBy(s.year_from, upTo); })};
      // Each move in two halves, the first ending in an arrow, so the
      // arrow sits halfway along the route and not under a pin.
      journeySegments(j, byId).forEach(function (s) {
        var mid = d3.geoInterpolate(s.from, s.to)(0.5);
        focus.append('path').attr('class', 'map-journey is-followed')
          .attr('stroke-width', FOCUS_WIDTH / k).attr('marker-end', 'url(#fha-map-arrow)')
          .attr('d', path({type: 'LineString', coordinates: [s.from, mid]}));
        focus.append('path').attr('class', 'map-journey is-followed')
          .attr('stroke-width', FOCUS_WIDTH / k)
          .attr('d', path({type: 'LineString', coordinates: [mid, s.to]}));
      });
      stopNumbers(j).forEach(function (n) {
        var p = byId[n.place];
        if (!p) return;
        numbers.append('g').datum(p).attr('transform', at([p.lon, p.lat], 1 / k))
          .append('text').attr('class', 'map-stop-number')
          .attr('y', -(pinRadius(p.events) + 4))
          .text(n.label);
      });
    }

    /* Show what had happened by the slider's year. A hidden pin is not
     * drawn at all, so the keyboard skips it too. */
    function showYear(year) {
      upTo = year;
      pins.classed('is-later', function (d) { return !shownBy(firstYear[d.id], upTo); });
      faint.selectAll('path').classed('is-later', function (s) { return !shownBy(s.year, upTo); });
      drawFollowed();
    }

    // Who has a journey, by the href their name links to.
    var journeyOf = {};
    journeys.forEach(function (j, i) { journeyOf[j.href] = i; });
    function followButton(href) {
      if (!(href in journeyOf)) return null;
      return function () { follow(journeyOf[href]); };
    }

    if (picker && journeys.length) {
      if (journeys.length > FAINT_MAX && picker.options.length) {
        picker.options[0].textContent = 'Choose a person';
      }
      journeys.forEach(function (j, i) {
        var option = el('option', j.life ? j.name + ' (' + j.life + ')' : j.name);
        option.value = String(i);
        picker.appendChild(option);
      });
      picker.addEventListener('change', function () {
        follow(picker.value === '' ? -1 : Number(picker.value));
      });
      if (picker.parentNode) picker.parentNode.hidden = false;
    }

    function choose(d) {
      if (followed >= 0) follow(-1);
      pins.classed('is-active', function (x) { return x === d; });
      showPlace(panel, d, journeys.length ? followButton : null);
    }
    pins.on('click', function (event, d) { choose(d); });
    pins.on('keydown', function (event, d) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        choose(d);
      }
    });

    // Pins and lines keep their size on screen as the map zooms.
    var zoom = d3.zoom().scaleExtent([1, 60]).on('zoom', function (event) {
      k = event.transform.k;
      layer.attr('transform', event.transform);
      pins.attr('transform', function (d) { return at([d.lon, d.lat], 1 / k); });
      numbers.selectAll('g').attr('transform', function (d) { return at([d.lon, d.lat], 1 / k); });
      faint.selectAll('path').attr('stroke-width', FAINT_WIDTH / k);
      focus.selectAll('path').attr('stroke-width', FOCUS_WIDTH / k);
    });
    svg.call(zoom);
    if (years) years.start(showYear);
    if (reset) {
      reset.hidden = false;
      reset.addEventListener('click', function () {
        svg.call(zoom.transform, d3.zoomIdentity);
      });
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {bounds: bounds, projectionKind: projectionKind, fitTarget: fitTarget,
                      makeProjection: makeProjection, pinRadius: pinRadius,
                      stopYears: stopYears, stopText: stopText, journeySegments: journeySegments,
                      stopNumbers: stopNumbers, leftOutText: leftOutText,
                      pinYears: pinYears, shownBy: shownBy, yearText: yearText,
                      playStart: playStart, nextPlayYear: nextPlayYear,
                      motionReduced: motionReduced};
    return;
  }

  document.addEventListener('DOMContentLoaded', function () {
    var box = document.getElementById('fha-map');
    var source = document.getElementById('fha-map-data');
    var panel = document.getElementById('fha-map-panel');
    if (!box || !source || !panel) return;
    // A copy missing a vendored file keeps the list under the map.
    if (!window.d3 || !window.topojson || !window.FHA_WORLD) return;
    var data;
    try {
      data = JSON.parse(source.textContent);
    } catch (e) {
      return;
    }
    if (!data || !data.places || !data.places.length) return;
    box.hidden = false;
    panel.hidden = false;
    draw(window.d3, window.topojson, window.FHA_WORLD, data, box, panel,
         document.getElementById('fha-map-reset'), document.getElementById('fha-map-journey'),
         data.journeys && data.journeys.length ? yearSlider(document, data.years) : null);
  });
})();
