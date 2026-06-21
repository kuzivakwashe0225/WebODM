PluginsAPI.Map.willAddControls([
    	'plant-height/build/PlantHeight.js',
    	'plant-height/build/PlantHeight.css'
	], function(args, PlantHeight){
	var tasks = [];
	var ids = {};
	
	for (var i = 0; i < args.tiles.length; i++){
		var task = args.tiles[i].meta.task;
		if (!ids[task.id]){
			tasks.push(task);
			ids[task.id] = true;
		}
	}

	// TODO: add support for map view where multiple tasks are available?
	if (tasks.length === 1){
		args.map.addControl(new PlantHeight({map: args.map, tasks: tasks}));
	}
});