PluginsAPI.Map.willAddControls([
    	'vegetation-indices/build/VegetationIndices.js',
    	'vegetation-indices/build/VegetationIndices.css'
	], function(args, VegetationIndices){
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
		args.map.addControl(new VegetationIndices({map: args.map, tasks: tasks}));
	}
});