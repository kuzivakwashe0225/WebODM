PluginsAPI.Map.willAddControls([
		'precise_agric/build/app.js',
		'precise_agric/build/app.css'
	], function(args, App){
	var tasks = [];
	var ids = {};

	for (var i = 0; i < args.tiles.length; i++){
		var task = args.tiles[i].meta.task;
		if (!ids[task.id]){
			tasks.push(task);
			ids[task.id] = true;
		}
	}

	// Single-task map views only (a Capture is one task)
	if (tasks.length === 1){
		new App(args.map, tasks[0]);
	}
});

// Adds "Upload Orthophoto (Precise Agric)" to each project's existing
// "Import" dropdown (next to the native "Assets / Backups" and "External
// Data" options) -- for a capture that's already a finished, georeferenced
// GeoTIFF, bypassing NodeODM entirely (see agri/capture.py). No JSX here:
// main.js is loaded as a plain script (not webpack-bundled), so we build
// the element with React.createElement directly.
PluginsAPI.Dashboard.addImportTaskItem(function(args){
	var handleClick = function(){
		// The input must actually be in the DOM (even if hidden) for a
		// synthetic .click() to reliably open the native file picker and
		// for its change event to fire in every browser -- a detached
		// element is not guaranteed to work.
		var input = document.createElement('input');
		input.type = 'file';
		input.accept = '.tif,.tiff';
		input.style.display = 'none';
		document.body.appendChild(input);

		var cleanup = function(){
			if (input.parentNode) input.parentNode.removeChild(input);
		};

		input.onchange = function(){
			var file = input.files[0];
			console.log('[precise_agric] file input changed:', file);
			if (!file){ cleanup(); return; }

			var defaultName = file.name.replace(/\.(tif|tiff)$/i, '');
			var name = window.prompt('Name this capture:', defaultName);
			if (name === null){ cleanup(); return; } // user cancelled

			// Acquisition date drives the Season Progress graphs. Default today.
			var today = new Date().toISOString().slice(0, 10);
			var captureDate = window.prompt('Capture date (YYYY-MM-DD) — when the imagery was flown:', today);
			if (captureDate === null){ cleanup(); return; } // user cancelled

			var formData = new FormData();
			formData.append('project', args.projectId);
			formData.append('name', name || file.name);
			formData.append('capture_date', captureDate || today);
			formData.append('orthophoto', file);

			console.log('[precise_agric] uploading orthophoto to /api/agri/captures/ ...');
			jQuery.ajax({
				url: '/api/agri/captures/',
				type: 'POST',
				data: formData,
				processData: false,
				contentType: false
			}).done(function(res){
				console.log('[precise_agric] upload OK:', res);
				cleanup();
				args.onNewTaskAdded();
			}).fail(function(xhr){
				console.error('[precise_agric] upload FAILED:', xhr.status, xhr.responseText);
				cleanup();
				var detail = (xhr.responseJSON && (xhr.responseJSON.detail || xhr.responseJSON.orthophoto)) || xhr.statusText;
				window.alert('Could not upload orthophoto: ' + detail);
			});
		};
		input.click();
	};

	return React.createElement('li', {key: 'precise-agric-upload'},
		React.createElement('a', {href: 'javascript:void(0)', onClick: handleClick},
			React.createElement('i', {className: 'fa fa-seedling fa-fw'}),
			' Upload Orthophoto (Precise Agric)'
		)
	);
});

// Adds "Get Satellite Imagery" as a sibling of "Upload Orthophoto" in the same
// Import dropdown -- the same mental slot a user already looks in to add
// imagery to a farm, per the UX review (stage-10-sentinel-roadmap.md /
// "How satellite fits into the everyday flow"). Never mentions Sentinel or
// Copernicus anywhere in the UI, per the branding decision locked in Stage 9.
//
// No JSX / no bundler here (matches the upload item above), so the modal is
// built with plain DOM + jQuery.ajax rather than a React component.
PluginsAPI.Dashboard.addImportTaskItem(function(args){
	var openSatelliteModal = function(){
		var todayIso = new Date().toISOString().slice(0, 10);
		var fromDate = new Date();
		fromDate.setDate(fromDate.getDate() - 30);
		var fromIso = fromDate.toISOString().slice(0, 10);

		var overlay = document.createElement('div');
		overlay.className = 'pa-sat-modal-overlay';
		overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); ' +
			'z-index:2000; display:flex; align-items:center; justify-content:center;';

		var box = document.createElement('div');
		box.style.cssText = 'width:380px; max-width:92vw; background:#fff; border-radius:10px; ' +
			'box-shadow:0 8px 30px rgba(0,0,0,0.3); overflow:hidden; font-size:13px;';
		overlay.appendChild(box);

		var head = document.createElement('div');
		head.style.cssText = 'background:linear-gradient(135deg,#0d47a1,#0a3579); color:#fff; ' +
			'padding:12px 16px; font-weight:700; display:flex; align-items:center; gap:8px;';
		head.innerHTML = '<i class="fa fa-satellite"></i> Get Satellite Imagery';
		box.appendChild(head);

		var body = document.createElement('div');
		body.style.cssText = 'padding:16px; display:flex; flex-direction:column; gap:12px;';
		box.appendChild(body);

		var fieldLabel = document.createElement('label');
		fieldLabel.style.cssText = 'font-size:11px; font-weight:700; text-transform:uppercase; ' +
			'letter-spacing:.03em; color:#888; display:block; margin-bottom:4px;';
		fieldLabel.textContent = 'Area';
		body.appendChild(fieldLabel);

		var fieldSelect = document.createElement('select');
		fieldSelect.className = 'form-control';
		fieldSelect.innerHTML = '<option value="">Whole farm</option>';
		body.appendChild(fieldSelect);

		// "Both as options" (UX review, 2026-08-14): whole farm is the default,
		// but any onboarded field can be targeted instead.
		jQuery.getJSON('/api/agri/fields/?project=' + args.projectId).done(function(fields){
			fields.forEach(function(f){
				var opt = document.createElement('option');
				opt.value = f.id;
				opt.textContent = f.name;
				fieldSelect.appendChild(opt);
			});
		});

		var dateLabel = document.createElement('label');
		dateLabel.style.cssText = fieldLabel.style.cssText;
		dateLabel.textContent = 'Date range';
		body.appendChild(dateLabel);

		var dateRow = document.createElement('div');
		dateRow.style.cssText = 'display:flex; gap:8px;';
		var fromInput = document.createElement('input');
		fromInput.type = 'date'; fromInput.className = 'form-control'; fromInput.value = fromIso;
		var toInput = document.createElement('input');
		toInput.type = 'date'; toInput.className = 'form-control'; toInput.value = todayIso;
		dateRow.appendChild(fromInput); dateRow.appendChild(toInput);
		body.appendChild(dateRow);

		// Available scenes (Stage 10 Phase 2): shows real options instead of
		// blindly trusting the automatic least-cloudy pick. Selecting one
		// narrows the pull to that exact day; "Auto" (the default) keeps the
		// original range-wide auto-pick behaviour unchanged.
		var scenesLabel = document.createElement('label');
		scenesLabel.style.cssText = fieldLabel.style.cssText;
		scenesLabel.textContent = 'Available scenes';
		body.appendChild(scenesLabel);

		var scenesList = document.createElement('div');
		scenesList.style.cssText = 'display:flex; flex-direction:column; gap:4px; max-height:140px; overflow-y:auto;';
		body.appendChild(scenesList);

		var selectedDate = null; // null = auto (least-cloudy in range)

		var renderScenes = function(scenes, errorMsg){
			scenesList.innerHTML = '';
			selectedDate = null;

			var makeOption = function(label, sub, date, picked){
				var row = document.createElement('div');
				row.style.cssText = 'display:flex; align-items:center; gap:8px; padding:5px 8px; ' +
					'border:1px solid ' + (picked ? '#0d47a1' : '#e0e0e0') + '; border-radius:6px; ' +
					'font-size:12px; cursor:pointer; background:' + (picked ? '#eaf1f9' : '#fff') + ';';
				row.innerHTML = '<b>' + label + '</b><span style="margin-left:auto; color:#888;">' + sub + '</span>';
				row.onclick = function(){
					selectedDate = date;
					Array.prototype.forEach.call(scenesList.children, function(el){
						el.style.borderColor = '#e0e0e0'; el.style.background = '#fff';
					});
					row.style.borderColor = '#0d47a1'; row.style.background = '#eaf1f9';
				};
				return row;
			};

			scenesList.appendChild(makeOption('Auto', 'least cloudy in range', null, true));

			if (errorMsg){
				var note = document.createElement('div');
				note.style.cssText = 'font-size:11px; color:#888;';
				note.textContent = errorMsg;
				scenesList.appendChild(note);
				return;
			}
			scenes.forEach(function(s){
				var cloudGood = s.cloud_cover_pct <= 20;
				var chip = (cloudGood ? '✓ ' : '⚠ ') + s.cloud_cover_pct + '% cloud';
				scenesList.appendChild(makeOption(s.date, chip, s.date, false));
			});
		};

		var checkAvailability = function(){
			if (!fromInput.value || !toInput.value || fromInput.value > toInput.value) return;
			scenesList.innerHTML = '<div style="font-size:11px; color:#888;"><i class="fa fa-circle-notch fa-spin"></i> Checking…</div>';
			var url = '/api/agri/satellite/availability/?project=' + args.projectId +
				'&date_from=' + fromInput.value + '&date_to=' + toInput.value +
				(fieldSelect.value ? '&field=' + fieldSelect.value : '');
			jQuery.ajax({type: 'GET', url: url}).done(function(scenes){
				renderScenes(scenes, scenes.length ? null : 'No scenes found in this range — auto-pick will still be attempted.');
			}).fail(function(xhr){
				renderScenes([], (xhr.responseJSON && xhr.responseJSON.detail) || 'Could not check availability.');
			});
		};
		fromInput.onchange = checkAvailability;
		toInput.onchange = checkAvailability;
		fieldSelect.onchange = checkAvailability;
		checkAvailability();

		var statusEl = document.createElement('div');
		statusEl.style.cssText = 'font-size:12px; color:#0d47a1; font-weight:600; min-height:16px;';
		body.appendChild(statusEl);

		var actions = document.createElement('div');
		actions.style.cssText = 'display:flex; gap:8px; justify-content:flex-end;';
		var cancelBtn = document.createElement('button');
		cancelBtn.className = 'btn btn-default btn-sm'; cancelBtn.textContent = 'Cancel';
		var goBtn = document.createElement('button');
		goBtn.className = 'btn btn-sm'; goBtn.style.cssText = 'background:#0d47a1; color:#fff;';
		goBtn.innerHTML = '<i class="fa fa-cloud-download-alt"></i> Get Satellite Imagery';
		actions.appendChild(cancelBtn); actions.appendChild(goBtn);
		body.appendChild(actions);

		var close = function(){
			if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
		};
		cancelBtn.onclick = close;
		overlay.onclick = function(e){ if (e.target === overlay) close(); };

		var pollTask = function(celeryTaskId){
			jQuery.ajax({type: 'GET', url: '/api/workers/check/' + celeryTaskId}).done(function(result){
				if (result.error){
					goBtn.disabled = false;
					statusEl.style.color = '#b3261e';
					statusEl.textContent = result.error;
				}else if (result.ready){
					statusEl.style.color = '#1b5e20';
					statusEl.textContent = 'Satellite imagery added. Review and approve its field boundary on the map.';
					console.log('[precise_agric] satellite imagery pull complete');
					args.onNewTaskAdded();
					setTimeout(close, 1400);
				}else{
					setTimeout(function(){ pollTask(celeryTaskId); }, 3000);
				}
			}).fail(function(){ setTimeout(function(){ pollTask(celeryTaskId); }, 3000); });
		};

		goBtn.onclick = function(){
			if (!fromInput.value || !toInput.value){
				statusEl.style.color = '#b3261e';
				statusEl.textContent = 'Pick a from/to date first.';
				return;
			}
			if (fromInput.value > toInput.value){
				statusEl.style.color = '#b3261e';
				statusEl.textContent = 'The start date must be before the end date.';
				return;
			}
			goBtn.disabled = true;
			statusEl.style.color = '#0d47a1';
			statusEl.textContent = 'Requesting satellite imagery… this can take a minute.';

			// A specific scene picked from "Available scenes" narrows the pull to
			// that exact day; "Auto" (selectedDate === null) keeps the original
			// range-wide least-cloudy auto-pick unchanged.
			var payload = {
				project: args.projectId,
				date_from: selectedDate || fromInput.value,
				date_to: selectedDate || toInput.value
			};
			if (fieldSelect.value) payload.field = fieldSelect.value;

			console.log('[precise_agric] requesting satellite imagery', payload);
			jQuery.ajax({
				type: 'POST', url: '/api/agri/satellite/imagery/', contentType: 'application/json',
				data: JSON.stringify(payload)
			}).done(function(res){
				pollTask(res.celery_task_id);
			}).fail(function(xhr){
				goBtn.disabled = false;
				statusEl.style.color = '#b3261e';
				statusEl.textContent = (xhr.responseJSON && xhr.responseJSON.detail) || xhr.statusText;
			});
		};

		document.body.appendChild(overlay);
	};

	return React.createElement('li', {key: 'precise-agric-satellite'},
		React.createElement('a', {href: 'javascript:void(0)', onClick: openSatelliteModal},
			React.createElement('i', {className: 'fa fa-satellite fa-fw'}),
			' Get Satellite Imagery'
		)
	);
});
