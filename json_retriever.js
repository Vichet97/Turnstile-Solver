const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

/**
 * Attempts to retrieve a JSON object with a 3-second timeout.
 * If the JSON object is not found within the timeout period, cleans up resources and terminates gracefully.
 * @returns {Promise<{success: boolean, data?: object, error?: string}>} Promise resolving to result object
 */
async function retrieveJSONWithTimeout() {
  let timeoutId;
  let resourcesCleanedUp = false;
  let dom = null;
  
  // Resource cleanup function
  const cleanupResources = () => {
    if (resourcesCleanedUp) return;
    resourcesCleanedUp = true;
    
    // Clear any pending timeouts
    if (timeoutId) {
      clearTimeout(timeoutId);
    }
    
    // Close DOM window if it exists
    if (dom && dom.window) {
      try {
        dom.window.close();
      } catch (e) {
        // Ignore cleanup errors
      }
    }
    
    // Force garbage collection if available
    if (global.gc) {
      global.gc();
    }
  };
  
  return new Promise((resolve) => {
    let jsonRetrieved = false;
    
    // Set up 3-second timeout
    timeoutId = setTimeout(() => {
      if (!jsonRetrieved) {
        cleanupResources();
        resolve({
          success: false,
          error: 'Timeout: JSON object not retrieved within 3 seconds'
        });
      }
    }, 3000);
    
    // JSON retrieval process
    const attemptJSONRetrieval = async () => {
      try {
        // Read the HTML file
        const pageUrl = 'https://goplay.ml/333837323363/Immortal-Samsara/';
        const htmlContent = fs.readFileSync('test.html', 'utf8');
        
        // Create JSDOM instance with request interception
        const virtualConsole = new VirtualConsole();
        virtualConsole.on('error', () => {}); // Silence error logs
        
        dom = new JSDOM(htmlContent, {
          runScripts: 'dangerously',
          resources: 'usable',
          pretendToBeVisual: true,
          url: pageUrl,
          virtualConsole: virtualConsole
        });
        
        const window = dom.window;
        
        // Intercept XMLHttpRequest to capture /title_search responses
        const originalXHR = window.XMLHttpRequest;
        
        // Function to extract required data from DOM
        const extractPageData = () => {
          const doc = window.document;
          
          // Extract title
          const titleElement = doc.getElementById('infotitle');
          const title = titleElement ? titleElement.textContent.split("Episode").shift().trim() : null;
          
          // Extract current episode
          const currentEpisode = window.episodeno;
          
          // Extract release date
          const releaseDateElement = doc.getElementById('publishinfo');
          const released_at = releaseDateElement ? releaseDateElement.textContent.split("on ").pop().trim() : null;
          
          // Extract description
          const descElement = doc.getElementById('desctext');
          const description = descElement ? descElement.textContent.trim() : null;
          
          // Extract episode count as number
          const episodeCountElement = doc.getElementById('episodecount');
          const episodeText = episodeCountElement ? episodeCountElement.textContent.trim() : '0';
          const episode = parseInt(episodeText.replace(/\D/g, '')) || 0;
          
          // Extract actors
          const actorElements = doc.querySelectorAll('.artists_entry_main_main');
          const actors = Array.from(actorElements).map(actor => {
            const nameElement = actor.querySelector('.artists_entry_name');
            const linkElement = nameElement ? nameElement.querySelector('a') : null;
            const characterElement = actor.querySelector('.artists_entry_character');
            const imageElement = actor.querySelector('.artists_img');
            
            // Only include actors with links starting with '/browse?artist='
            let url = null;
            if (linkElement && linkElement.getAttribute('href') && linkElement.getAttribute('href').includes('/browse?artist=')) {
              url = linkElement.href;
            }
            
            return {
              id: url.split('/browse?artist=').pop(),
              name: nameElement ? nameElement.textContent.trim() : null,
              character: characterElement ? characterElement.textContent.trim() : null,
              image: imageElement ? imageElement.src : null,
              url: url
            };
          });
          
          // Extract MyDramaList link
          const externalLinkElement = doc.getElementById('external_link_wiki');
          let mydramalist = null;
          if (externalLinkElement) {
            const links = externalLinkElement.querySelectorAll('a');
            for (const link of links) {
              if (link.textContent && link.textContent.toLowerCase().includes('mydramalist')) {
                mydramalist = link.href;
                break;
              }
            }
          }
          
          // Extract episodes list
          const episodeElements = doc.querySelectorAll('#episodesodd');
          const episodes = Array.from(episodeElements).map(ep => {
            const linkElement = ep.querySelector('a');
            const imageElement = ep.querySelector('img');
            const numberElement = ep.querySelector('[id="episodesnumber"]');
            
            let episodeNumber = null;
            if (numberElement) {
              const numberText = numberElement.textContent.trim();
              const match = numberText.match(/\d+/);
              episodeNumber = match ? parseInt(match[0]) : null;
            }
            
            return {
              url: linkElement ? linkElement.href : null,
              image: imageElement ? imageElement.src : null,
              episode: episodeNumber
            };
          });

          // Extract servers from title_search requests
          let servers = [];
          
          // Wait for server extraction to complete
          return new Promise((resolve) => {

            window.XMLHttpRequest = function() {
            const xhr = new originalXHR();
            const originalOpen = xhr.open;
            const originalSend = xhr.send;
            
            let requestUrl = '';
            
            xhr.open = function(method, url, ...args) {
              requestUrl = url;
              return originalOpen.call(this, method, url, ...args);
            };
            
            xhr.send = function(...args) {
              if (requestUrl.includes('/title_search')) {
                xhr.addEventListener('load', function() {
                  if (xhr.status === 200) {
                    try {
                      // Parse HTML response to extract server options
                      const htmlResponse = xhr.responseText;
                      
                      // Create a temporary DOM to parse the HTML
                      const tempDiv = window.document.createElement('div');
                      tempDiv.innerHTML = htmlResponse;
                      
                      // Extract option elements
                      const options = tempDiv.querySelectorAll('option');
                      options.forEach(option => {
                        const optionId = option.id;
                        if (optionId && optionId.startsWith('sourceid-')) {
                          const platform = optionId.replace('sourceid-', '');
                          servers.push({
                            platform: platform,
                            id: option.value
                          });
                        }
                      });
                    } catch (e) {
                    }
                  }

                  const currentPlatform = servers.find(server=>pageUrl.includes(server.id));

                  resolve({
                    id: currentPlatform?.id,
                    platform: currentPlatform?.platform,
                    title,
                    currentEpisode,
                    released_at,
                    description,
                    episode,
                    actors,
                    mydramalist,
                    episodes,
                    servers,
                    url: pageUrl
                  });

                });
              }
              return originalSend.call(this, ...args);
            };
            
            return xhr;
          };

          window.title_search_api(window.titleSearchApi);

          });
        };
        
        // Mock JWPlayer to capture configuration
        window.jwplayer = function(elementId) {
          return {
            setup: async function(config) {
              if (!jsonRetrieved) {
                jsonRetrieved = true;
                clearTimeout(timeoutId);
                
                // Extract page data after configuration is captured
                const pageData = await extractPageData();
                
                resolve({
                  success: true,
                  data: config,
                  pageData: pageData,
                  currentEpisodeStreaming: config
                });
              }
            }
          };
        };
        
      } catch (error) {
        if (!jsonRetrieved) {
          jsonRetrieved = true;
          clearTimeout(timeoutId);
          cleanupResources();
          
          resolve({
            success: false,
            error: `Failed to retrieve JSON: ${error.message}`
          });
        }
      }
    };
    
    // Start the retrieval process
    attemptJSONRetrieval();
  });
}

// Export the function for external use
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { retrieveJSONWithTimeout };
}

// If running directly, execute the function
if (require.main === module) {
  (async () => {
    const result = await retrieveJSONWithTimeout();
    
    if (result.success) {
      // Output the structured data as requested
      const output = result.pageData;
      
      console.log(JSON.stringify(output, null, 2));
    } else {
      console.log('Failed to retrieve data:', result.error);
    }
  })();
}